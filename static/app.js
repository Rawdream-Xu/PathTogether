/* =========================================================================
   SVS 病理图像查看器 —— 前端逻辑（OpenSeadragon + ROI + 项目/标注）
   ========================================================================= */
(function () {
  "use strict";

  // 中英双语：i18n.js 在本脚本之前加载，提供 window.HP_I18N.t
  function t(key, vars) {
    return window.HP_I18N ? window.HP_I18N.t(key, vars) : key;
  }

  // 本轮新增的 i18n 文案（暂未落入 i18n.js 字典的兜底表）。优先取 i18n.js 的值；
  // 缺失时按当前界面语言走本地兜底，避免回退成 key 本身。
  var _EXTRA_I18N = {
    "ai.fork.sending": { zh: "发送中…", en: "Sending…" },
    "anno.private.badge": { zh: "私有", en: "Private" },
    // 上传错误码 → 可读文案（上传修复 U1：服务端返回的机器码不再原样透出；
    // U3 补充 V2 分片机器码与三段状态文案）
    "upload.err.csrf": { zh: "登录状态已失效，请刷新页面后重试", en: "Session expired, please refresh and retry" },
    "upload.err.guard": { zh: "上传配额服务暂不可用，请稍后重试", en: "Upload quota service unavailable, please retry later" },
    "upload.err.name": { zh: "名称不可用，请重命名后重试", en: "Name unavailable, please rename and retry" },
    "upload.err.too_large": { zh: "文件超过单次上传上限", en: "File exceeds the per-request upload limit" },
    "upload.err.disk": { zh: "服务器磁盘空间不足", en: "Insufficient disk space on server" },
    "upload.err.offset_mismatch": { zh: "分片偏移与服务端不一致，正在对齐重传", en: "Chunk offset out of sync, realigning and retrying" },
    "upload.err.hash_mismatch": { zh: "内容校验失败（哈希不匹配），请重新上传", en: "Integrity check failed (hash mismatch), please re-upload" },
    "upload.err.state_conflict": { zh: "上传任务状态冲突，请刷新页面后重试", en: "Upload task state conflict, please refresh and retry" },
    "upload.err.use_legacy": { zh: "ZIP/MRXS 请使用单请求上传", en: "ZIP/MRXS must use the single-request upload" },
    "upload.err.invalid_slide": { zh: "文件校验失败：不是有效的切片文件", en: "Validation failed: not a valid slide file" },
    // 上传修复 A0：新增稳定机器码的中文文案（保留机器码供排障）
    "upload.err.slide_open_unsupported": { zh: "文件格式不受支持：服务端无法按切片格式打开该文件", en: "Unsupported format: the server cannot open this file as a slide" },
    "upload.err.slide_open_failed": { zh: "切片解析失败：文件可能损坏或不完整，请重试上传", en: "Slide parsing failed: the file may be corrupted or incomplete, please retry" },
    "upload.err.commit_retry": { zh: "服务端校验暂时失败，请稍后重试提交", en: "Server validation failed temporarily, retry commit later" },
    "upload.err.size_mismatch": { zh: "文件大小与声明不符，请重新上传", en: "File size mismatch, please re-upload" },
    // U3 三段状态（§3.5：正在传输 → 服务端校验 → 入库完成）
    "upload.stage.converting": { zh: "切片转换中", en: "Converting slide" },
    "upload.stage.done": { zh: "上传成功", en: "Upload successful" },
    "upload.stage.failed": { zh: "上传失败", en: "Upload failed" },
    "upload.err.conversion": { zh: "切片转换失败", en: "Slide conversion failed" },
    // COS 直传 Phase 4（§5 阶段文案固定六段 + 稳定机器码；i18n.js 为主源，
    // 此处兜底）。上传 100% ≠ 可查看，绝不合成全流程百分比
    "upload.cos.stage.waiting_space": { zh: "服务器正在处理…", en: "Server processing…" },
    "upload.cos.stage.uploading": { zh: "正在上传", en: "Uploading" },
    "upload.cos.stage.awaiting_server": { zh: "服务器正在处理…", en: "Server processing…" },
    "upload.cos.stage.downloading": { zh: "服务器正在处理", en: "Server processing" },
    "upload.cos.stage.validating": { zh: "服务器正在处理…", en: "Server processing…" },
    "upload.cos.stage.processing": { zh: "服务器正在处理…", en: "Server processing…" },
    "upload.cos.stage.readiness": { zh: "服务器正在处理…", en: "Server processing…" },
    "upload.cos.stage.viewable": { zh: "服务器正在处理…", en: "Server processing…" },
    "upload.cos.queue": { zh: "第 {n} 位", en: "position {n}" },
    "upload.cos.cancel": { zh: "取消", en: "Cancel" },
    "upload.cos.cancelled": { zh: "已取消", en: "Cancelled" },
    "upload.cos.retry": { zh: "重试", en: "Retry" },
    "upload.cos.recheck": { zh: "重新检查状态", en: "Re-check status" },
    "upload.cos.resume_hint": { zh: "上传未完成；重新选择同名文件可续传", en: "Upload unfinished; re-select the same file to resume" },
    "upload.cos.resume_confirm": { zh: "检测到「{name}」有未完成的云端直传任务，续传已上传的分块？", en: "An unfinished cloud upload for \"{name}\" exists. Resume from the uploaded parts?" },
    "upload.cos.err.format_unsupported": { zh: "不支持该文件格式", en: "Unsupported file format" },
    "upload.cos.err.too_large": { zh: "文件超过平台上限", en: "File exceeds the platform size limit" },
    "upload.cos.err.pool_config": { zh: "服务端存储配置暂不能接纳该大小，请联系运维", en: "Server storage config cannot accept this size yet; contact the operator" },
    "upload.cos.unavailable": { zh: "上传暂不可用（云直传能力未开放）", en: "Upload unavailable (cloud direct-upload capability is off)" },
    "upload.cos.items": { zh: "已加入工作台 {n}/{m} 项", en: "{n}/{m} items added to the workbench" },
    "upload.cos.conv_state": { zh: "转换中（{s}）", en: "Converting ({s})" },
    "upload.cos.err.waiting_limit": { zh: "已有等待中的云端直传任务", en: "Another cloud upload is already waiting" },
    "upload.cos.err.state": { zh: "任务状态冲突，请刷新页面后重试", en: "Job state conflict; please refresh and retry" },
    // 跨账号归属核对（P1 cross-account 2026-10-15）：服务端按会话核对
    // expected_account 不符（i18n.js 为主源，此处兜底）
    "upload.cos.err.account_mismatch": { zh: "登录账号已变化：本页身份与当前会话不一致，未创建上传任务。请刷新页面重新登录后再上传", en: "Your signed-in account changed: this page's identity no longer matches the session and no upload was created. Refresh and sign in again before uploading" },
    "upload.cos.err.rate": { zh: "签名请求过于频繁，请稍后重试", en: "Signing rate limited; please retry later" },
    "upload.cos.err.reconcile": { zh: "云端容量对账中，暂不可继续，请稍后重试", en: "Cloud capacity reconciliation in progress; retry later" },
    // upload-liveness（0084）：vanish client 终态与校验细节文案（i18n.js 为主源，此处兜底）
    "upload.cos.err.client_gone": { zh: "该上传任务因长时间无活动已自动取消：请重新发起上传", en: "This upload was cancelled automatically after prolonged inactivity; please start the upload again" },
    "upload.cos.err.client_idle_timeout": { zh: "该上传长时间无进展已自动取消：请重新发起上传", en: "This upload was cancelled after a long idle period; please start the upload again" },
    "upload.cos.err.validation": { zh: "文件校验失败：内容不是有效的切片文件", en: "Validation failed: the content is not a valid slide file" },
    "upload.cos.err.raster_mismatch": { zh: "文件内容与扩展名不符（例如 PNG/WebP 被存成了 .jpg）：请用正确格式重新保存或改名后再上传", en: "File content does not match its extension (e.g. a PNG/WebP saved as .jpg); re-save or rename it correctly and upload again" },
    "upload.cos.err.magic": { zh: "文件内容与扩展名不符（检测到 {type}）：请用正确格式重新保存后再上传", en: "File content does not match its extension (detected {type}); re-save it in the correct format and upload again" },
    // 复核 8428f7f0：跨标签内容锁等待（另一标签正在上传同一文件；i18n.js 为主源，此处兜底）
    "upload.cos.other.tab": { zh: "另一个标签页正在上传同一文件，等待中…（可取消）", en: "Another tab is uploading this file — waiting… (you can cancel)" },
    // U1 字节级上传进度（i18n.js 为主源，此处兜底）
    "upload.cos.bytes": { zh: "已传输 {done} / {total}", en: "{done} / {total} transferred" },
    "upload.cos.sent_all": { zh: "数据已发送，等待确认", en: "All data sent, awaiting confirmation" },
    "upload.cos.retrying": { zh: "正在重试", en: "Retrying" },
    "upload.cos.err.plan": { zh: "上传计划与文件大小不一致，已停止；请重新上传", en: "Upload plan does not match the file size; stopped — please re-upload" },
    // 存储配额超限（2026-11-04；i18n.js 为主源，此处兜底）：{quota} 取该
    // 用户实际配额；拿不到数值走 generic（不带数字）。
    "upload.cos.stage.quota_full": { zh: "存储空间已满", en: "Storage full" },
    "upload.cos.err.quota": { zh: "存储空间已满（上限 {quota}）。如需更多容量，请联系管理员。", en: "Storage is full (limit {quota}). Contact an administrator if you need more space." },
    "upload.cos.err.quota.generic": { zh: "存储空间已满。如需更多容量，请联系管理员。", en: "Storage is full. Contact an administrator if you need more space." },
    // 2026-10-20 用户决定：独立转换器只做本地转换与保存；工作台上传不再
    // 跳转/弹窗交接——需要转换的文件在面板不可用时行内说明 + 重试/支持。
    "upload.kfb.unavailable": { zh: "该文件需要先转换后上传：本机转换暂不可用，可重试或申请支持该格式", en: "This file must be converted before upload: local conversion is unavailable right now — retry or request format support" },
    // 上传面板内转换（docs/upload-inline-convert-design-20261015.md；i18n.js 为主源，
    // 此处兜底：app.js 的 tt() 在无 HP_I18N 词条时回落到本表）
    "upload.stage.pct": { zh: "（{pct}%）", en: " ({pct}%)" },
    "upload.step.prepare": { zh: "正在准备上传文件", en: "Preparing upload" },
    "upload.step.converting": { zh: "正在准备上传文件（格式转换中）", en: "Preparing upload (converting)" },
    "upload.inline.stage.queued": { zh: "正在准备上传文件", en: "Preparing upload" },
    "upload.inline.stage.rejected": { zh: "已拒绝：内容或格式不受支持", en: "Rejected: content or format not supported" },
    "upload.inline.stage.notAccepted": { zh: "上传词表不含该格式", en: "This format is not in the upload vocabulary" },
    "upload.inline.stage.resumable": { zh: "未完成的转换", en: "Unfinished conversion" },
    "upload.inline.stage.settled": { zh: "已收口（本地产物仍占用空间）", en: "Settled (local artifacts still use space)" },
    "upload.inline.stage.disk": { zh: "等待确认磁盘空间", en: "Waiting for disk confirmation" },
    "upload.inline.stage.convertFailed": { zh: "转换未完成", en: "Conversion not finished" },
    "upload.inline.stage.engineUnsupported": { zh: "该变体暂不支持转换", en: "This variant cannot be converted yet" },
    "upload.inline.stage.requeue": { zh: "识别修正：该文件需先在本机转换", en: "Correction: convert this file on this machine first" },
    "upload.inline.note.keepopen": { zh: "请保持页面打开", en: "keep this page open" },
    "upload.inline.note.committed": { zh: "已提交的进度可在刷新后继续", en: "Committed progress can be resumed after a refresh" },
    "upload.inline.rejected.magic": { zh: "内容与扩展名不符（检测到 {type}）", en: "Content does not match the extension (detected {type})" },
    "upload.inline.needBundle.mrxs": { zh: "MRXS 需要完整包：.mrxs 主文件 + 同名数据文件夹（内含 Slidedat.ini 与全部数据文件）。可以先选 .mrxs 主文件、再按提示选择同名文件夹；也可以用「选择文件夹」选择包含两者的文件夹，或拖入整个文件夹", en: "MRXS needs the whole bundle: the .mrxs entry plus its same-named data folder (with Slidedat.ini and all data files). Pick the .mrxs file first and follow the prompt to select the same-named folder; or use 'Select folder' on the folder holding both, or drop the whole folder" },
    "upload.inline.needBundle.vms": { zh: "VMS 需要完整包：.vms 入口 + 同目录全部 tile JPEG。请用「选择文件夹」或拖入整个文件夹", en: "VMS needs the whole bundle: the .vms entry plus every sibling tile JPEG. Use 'Select folder' or drop the whole folder" },
    // MRXS 协助（先选 .mrxs → 提示补选同名数据文件夹；i18n.js 为主源，此处兜底）
    "upload.inline.stage.needFolder": { zh: "等待选择同名数据文件夹", en: "Waiting for the same-named data folder" },
    "upload.inline.needFolder.mrxs": { zh: "这张切片还需要同名数据文件夹，请选择「{stem}」文件夹。", en: "This slide also needs its same-named data folder: please select the “{stem}” folder." },
    "upload.inline.needFolder.notDataFolder": { zh: "所选文件夹「{folder}」不是「{stem}」的数据文件夹（缺少 Slidedat.ini）。请重新选择文件夹。", en: "The selected folder “{folder}” is not the data folder of “{stem}” (Slidedat.ini is missing). Please choose a different folder." },
    "upload.inline.needFolder.readFailed": { zh: "无法读取所选文件夹的内容，请重新选择。", en: "The selected folder could not be read; please choose again." },
    "upload.inline.needFolder.tooMany": { zh: "所选文件夹的成员数超过上限 {n}，请确认选的是这张切片的数据文件夹。", en: "The selected folder has more than {n} members; please make sure it is this slide's data folder." },
    "upload.inline.needFolder.retry": { zh: "所选文件夹缺少需要的文件，请重新选择「{stem}」文件夹。", en: "The selected folder is missing required files; please choose the “{stem}” folder again." },
    "upload.inline.needFolder.retryMissing": { zh: "所选文件夹缺少 {missing}，请重新选择「{stem}」文件夹。", en: "The selected folder is missing {missing}; please choose the “{stem}” folder again." },
    "upload.saveproj.notice.text": { zh: "转换页有一个结果等待保存到我的项目", en: "The converter page has a result waiting to be saved to your projects" },
    "upload.saveproj.notice.continue": { zh: "继续保存", en: "Continue the save" },
    "upload.saveproj.notice.dismiss": { zh: "忽略", en: "Dismiss" },
    "upload.saveproj.fail.prefix": { zh: "无法继续保存到项目：", en: "Cannot continue saving to a project: " },
    "upload.saveproj.reason.capabilityOff": { zh: "面板内上传未开启", en: "in-panel upload is off" },
    "upload.saveproj.reason.jobMissing": { zh: "本机没有找到该任务（可能已被清理）", en: "the job was not found on this machine (it may have been cleared)" },
    "upload.saveproj.reason.notReady": { zh: "任务还没有完成转换", en: "the job has not finished converting" },
    "upload.saveproj.reason.alreadyUploaded": { zh: "该任务已经上传过", en: "that job was already uploaded" },
    "upload.inline.needFolder.toast": { zh: "所选文件夹缺少文件，请重新选择「{stem}」文件夹", en: "The selected folder is missing files; choose the “{stem}” folder again" },
    "upload.inline.needFolder.missingMore": { zh: "{names} 等 {n} 个文件", en: "{names}, and {n} more" },
    "upload.inline.companion.mismatch": { zh: "所选文件夹「{folder}」与切片「{stem}」不同名，仍然使用吗？", en: "The selected folder “{folder}” does not match the slide name “{stem}”. Use it anyway?" },
    "upload.inline.companion.use": { zh: "使用此文件夹", en: "Use this folder" },
    "upload.inline.companion.hint": { zh: "浏览器会请求允许读取该文件夹。", en: "The browser will ask for permission to read this folder." },
    "upload.inline.companion.unsupported": { zh: "当前浏览器无法在这里选择文件夹；请改用「选择文件夹」按钮选择完整包，或直接拖入整个文件夹。", en: "This browser cannot choose folders here; use “Select folder” on the complete bundle, or drag the whole folder here." },
    "upload.inline.action.chooseFolderNamed": { zh: "选择「{stem}」文件夹", en: "Choose the “{stem}” folder" },
    "upload.inline.err.disk": { zh: "磁盘空间预检未通过", en: "Disk space precheck failed" },
    "upload.inline.err.quota": { zh: "浏览器存储配额不足：请腾出空间，或改在转换器页转换后本地保存", en: "Browser storage quota is insufficient: free some space, or convert on the converter page and save locally" },
    "upload.inline.err.profile": { zh: "当前资源档位内存预算不足", en: "This resource profile's memory budget is too small" },
    "upload.inline.err.unsupported": { zh: "该文件不支持本机转换（已按原样上传或见下方说明）", en: "This file cannot be converted on this machine (uploaded as-is or see the note below)" },
    "upload.inline.err.busy": { zh: "另一个标签页正在执行本机转换，请稍后重试", en: "Another tab is running a local conversion; please retry shortly" },
    "upload.inline.err.evicted": { zh: "本地任务目录已被浏览器清理，请重新选择文件", en: "The local job directory was evicted by the browser; please re-select the file" },
    "upload.inline.err.resume": { zh: "本地任务与当前设置不符，请重新选择文件后重试", en: "The local job no longer matches the current settings; please re-select the file" },
    "upload.inline.err.io": { zh: "本地读写错误：请重试", en: "Local I/O error: please retry" },
    "upload.inline.err.magic": { zh: "内容与扩展名不符：请用正确格式重新保存后再上传", en: "Content does not match the extension: re-save it in the correct format" },
    "upload.inline.err.notViewable": { zh: "平台暂不支持查看该转换产物：请改用「保存到本机」", en: "The platform cannot view this converted artifact yet: use 'Save to this machine'" },
    "upload.inline.err.network": { zh: "网络中断：产物已保留，点「继续上传」续传（已传分块会跳过）", en: "Network interrupted: the artifact is kept; click 'Continue upload' to resume (uploaded parts are skipped)" },
    "upload.inline.err.part": { zh: "部分分块上传失败：点「继续上传」续传（已确认分块会跳过）", en: "Some parts failed to upload: click 'Continue upload' to resume (confirmed parts are skipped)" },
    "upload.inline.err.needs.convert": { zh: "该格式需先在本机转换后上传", en: "This format must be converted on this machine before uploading" },
    "upload.inline.disk.confirm": { zh: "继续（我已确认）", en: "Continue (I understand)" },
    "upload.inline.action.continue": { zh: "继续", en: "Continue" },
    "upload.inline.action.continueUpload": { zh: "继续上传", en: "Continue upload" },
    "upload.inline.action.reselect": { zh: "重新选择文件", en: "Re-select file" },
    "upload.inline.action.reselectFolder": { zh: "重新选择文件夹", en: "Choose another folder" },
    "upload.inline.action.chooseFolder": { zh: "选择文件夹", en: "Select folder" },
    "upload.inline.action.discard": { zh: "删除本地产物", en: "Delete local artifacts" },
    "upload.inline.action.discard.published": { zh: "删除本地产物（平台已上传的切片不受影响）", en: "Delete local artifacts (uploaded slides are unaffected)" },
    "upload.inline.action.requestSupport": { zh: "申请支持该格式", en: "Request support for this format" },
    "upload.inline.action.saveLocal": { zh: "保存到本机", en: "Save to this machine" },
    "upload.inline.freed": { zh: "释放 {bytes}", en: "frees {bytes}" },
    "upload.inline.needDisk": { zh: "浏览器只报告了 ~{available} 可用，本任务预计需要 ~{need}", en: "The browser reports only ~{available} available; this job needs ~{need}" },
    "upload.inline.account.mismatch": { zh: "归属另一账号，等待确认", en: "Belongs to another account — awaiting confirmation" },
    "upload.inline.account.owner": { zh: "此记录由 {owner} 创建，当前登录是另一账号。可确认后改用当前账号继续（原上传仍归 {owner}），或删除本地产物", en: "This record was created by {owner}; you are signed in as a different account. Confirm to continue with the current account (the original upload stays with {owner}), or delete the local artifacts" },
    "upload.inline.action.rebindAccount": { zh: "改用当前账号继续", en: "Continue with the current account" },

    // 升级 C（§6.1）：矩形工具文案（i18n.js 为主源；此处兜底）
    "roi.rect.tip": { zh: "矩形工具：在视野中拖出矩形，或输入宽高后点击中心放置；拖内部平移、边/角调整大小；Escape 取消",
                      en: "Rectangle tool: drag in the view, or enter width/height then click to place; drag inside to move, edges/corners to resize; Escape cancels" },
    "roi.cancelled": { zh: "已取消未保存的选区", en: "Unsaved selection cancelled" },
    "roi.input.invalid": { zh: "矩形尺寸非法或超出图像范围，已保留上次的合法框",
                           en: "Invalid rectangle size or out of image bounds; kept the last valid box" },
    // Wave 3（普通图片兼容）：无物理标尺语义（i18n.js 为主源；此处兜底，未改 i18n.js）
    "roi.no.scale.hint": { zh: "该图片无物理标尺，仅支持像素（px）测量",
                           en: "This image has no physical scale; only pixel (px) measurements are available" },
    "slide.meta.no.scale": { zh: "无物理标尺", en: "no physical scale" },
    "edit.conflict": { zh: "该标注已被他人修改（当前 revision {rev}），已显示当前版本；请基于最新版本重新编辑",
                       en: "This annotation was modified by someone else (current revision {rev}); showing the current version — please re-edit on top of it" },

    // 改版第四轮 §1.2：图标按钮 aria 名称（i18n.js 为主源，此处兜底）
    "tb.rect.aria": { zh: "矩形", en: "Rectangle" },
    "tb.anno.arrow.aria": { zh: "箭头", en: "Arrow" },
    "tb.anno.free.aria": { zh: "描图", en: "Trace" },

    // 改版第四轮 §3：反馈问题（i18n.js 为主源，此处兜底）
    "fbk.entry": { zh: "反馈问题", en: "Feedback" },
    "fbk.title": { zh: "反馈问题", en: "Report a problem" },
    "fbk.desc.label": { zh: "问题描述", en: "Description" },
    "fbk.desc.ph": { zh: "请描述遇到的问题或建议（10–4000 字）", en: "Describe the problem or suggestion (10–4000 characters)" },
    "fbk.privacy.hint": { zh: "将附带最近的操作摘要（页面路径、点击的控件、接口状态码、报错）；不含输入内容、密码、切片图像、Cookie 或查询串。",
                          en: "Attaches a summary of recent activity (page paths, control clicks, API status codes, errors); never your typed input, passwords, slide images, cookies, or query strings." },
    "fbk.server.hint": { zh: "服务端还会附带账号、额度、应用版本、最近 24 小时的审计摘要和任务状态，供管理员排查。", en: "The server also attaches your account, allowance, app version, audit summaries from the last 24 hours and recent task statuses for admin diagnostics." },
    "fbk.preview.toggle": { zh: "查看将附带的信息", en: "See what will be attached" },
    "fbk.send": { zh: "发送", en: "Send" },
    "fbk.sending": { zh: "发送中…", en: "Sending…" },
    "fbk.success": { zh: "已收到，谢谢反馈", en: "Received — thank you for the feedback" },
    "fbk.err.short": { zh: "描述至少 10 个字", en: "Please write at least 10 characters" },
    "fbk.err.long": { zh: "描述最多 4000 字", en: "Please keep the description within 4000 characters" },
    "fbk.err.rate": { zh: "发送太频繁，请 {time}再试", en: "Too many requests; please retry {time}" },
    "fbk.err.rate.later": { zh: "稍后", en: "later" },
    "fbk.err.rate.seconds": { zh: "{n} 秒后", en: "in {n} seconds" },
    "fbk.err.rate.minutes": { zh: "{n} 分钟后", en: "in {n} minutes" },
    "fbk.err.too_large": { zh: "附带内容过大，请精简描述后重试", en: "Payload too large; please shorten the description and retry" },
    "fbk.err.generic": { zh: "发送失败（{status}），请稍后重试", en: "Sending failed ({status}); please retry later" },
    "fbk.err.network": { zh: "网络异常，发送失败，请稍后重试", en: "Network error — sending failed; please retry later" },
  };
  function tt(key, vars) {
    try {
      var s = window.HP_I18N && window.HP_I18N.t(key, vars);
      if (s && s !== key) return s;
    } catch (e) {}
    var lang = (window.HP_I18N && window.HP_I18N.getLang()) || "zh";
    var e = _EXTRA_I18N[key];
    var raw = (e && (e[lang] || e.zh)) || key;
    // COS 直传引入：兜底文案同样支持 {n}/{name} 形参（照 i18n.js fmt 语义）；
    // 不传 vars 的既有调用方不受影响
    if (vars) {
      raw = String(raw).replace(/\{(\w+)\}/g, function (_, k) {
        return Object.prototype.hasOwnProperty.call(vars, k) ? String(vars[k]) : ("{" + k + "}");
      });
    }
    return raw;
  }

  // ---------- 全局状态 ----------
  var state = {
    slide: null,          // 当前切片 {id,name,display_name,width,height,mppX,mppY,mppSource,revision}
                          // id=slide_id 唯一操作键（P2 合同 §5.1）；name=legacy 名（显示/快照）
    mppX: null,           // 当前生效的 µm/px
    // 升级 C（§6.1）：单一「矩形」入口。roiMode 为工具激活标志（null|"rect"）；
    // 选区本身是 level-0 像素矩形 roi={x,y,w,h}（权威几何，§6.2）。
    roiMode: null,        // null | "rect"
    roi: { x: 0, y: 0, w: 0, h: 0 },
    roiLockRatio: false,  // 锁定宽高比（默认不锁定）
    roiUnit: "mm",        // 设置区单位：mm | um | px
    roiPreset: "",        // "" | "6" | "6.5"（mm 预设只填数值不强制锁定）
    rotation: 0,
    flipped: false,       // 是否水平翻转（镜像）
    drawMode: null,       // null | "arrow" | "freehand"（与 roiMode 互斥）
    showAnno: false,      // 是否在画布层显示已保存标注
    focusAnno: null,      // null=显示全部；否则只显示该条标注（flatItems 中的引用；
                          // §6.1 起只由 ⋯「只看此标记」设置，选中不再隐藏其它标记）
    roiGeometryLocked: false, // §6.3：临时框草稿位置锁（draft.geometryLocked，
                              // 只对当前草稿有效；保存为标记时随 POST 带上）
    polygonVertexEdit: null,  // §6.2：polygon「编辑轮廓点」模式的目标 item
    channelReopening: false, // 通道配色重开（同一切片换 TileSource，非新切片）
    // 上次已发 slide.opened 的 "id|revision" 键：去重依据（见 emitSlideOpened；
    // P2 合同 §5.5：键切 id，旧后端无 id 时回落 name）
    lastSlideOpenedKey: null,
  };

  // ---------- 401 认证处理 ----------
  // fetch 包装：
  //  - 非安全方法自动附带 X-CSRF-Token（统一 CSRF 设施：非 HttpOnly 的 csrf_token
  //    cookie 与 session 绑定，双提交校验）；
  //  - 响应 401 且 body 含 auth_required 时跳登录页。
  // 对现有调用透明——仍返回 Response，调用方照常 .json()/.ok 判断。
  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function apiFetch(url, opts) {
    opts = opts || {};
    var method = (opts.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
      var tok = csrfToken();
      if (tok) {
        var headers = Object.assign({}, opts.headers || {});
        if (!headers["X-CSRF-Token"]) { headers["X-CSRF-Token"] = tok; }
        opts.headers = headers;
      }
    }
    return fetch(url, opts).then(function (resp) {
      if (resp.status === 401) {
        // 尝试读 body 判断是否 auth_required（不消费主响应流：克隆一份）。
        // D2（2026-09-10）起 401 body 为 {error: 中文, code: "auth_required"}，
        // 机器码在 code 字段；兼容读取旧形态 error 字段。
        return resp.clone().json().then(
          function (body) {
            if (body && (body.code === "auth_required" ||
                         body.error === "auth_required")) {
              location.href = "/login?next=" + encodeURIComponent(location.pathname);
            }
            return resp;
          },
          function () { return resp; }  // body 非 JSON，原样返回
        );
      }
      return resp;
    });
  }

  // 活性探测专用 fetch：与 apiFetch 同 CSRF 语义，但 401 **绝不跳转**。
  // upload-liveness 复核：心跳是分块在途期间的在场证据，不是状态推进请求；
  // 用 apiFetch 会在分块上传中途因 401 触发 location.href="/login"，把页面
  // 导航走并丢掉在传的上传行/本地产物。授权问题由分块签名这一必经控制请求
  // 暴露（工具页 pageApiFetch 从一开始就是不跳转语义）。
  function apiFetchNoRedirect(url, opts) {
    opts = opts || {};
    var method = (opts.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
      var tok = csrfToken();
      if (tok) {
        var headers = Object.assign({}, opts.headers || {});
        if (!headers["X-CSRF-Token"]) { headers["X-CSRF-Token"] = tok; }
        opts.headers = headers;
      }
    }
    return fetch(url, opts);
  }

  // ---------- 用户反馈记录器桥（改版第四轮 §3） ----------
  // window.HP_FEEDBACK 由 static/feedback-recorder.js 在本脚本之前装载
  // （fetch/console/error 包装必须尽早）。记录器缺失（demo 页等）时全部
  // 为空操作；桥自身任何异常都吞掉，绝不影响主流程。
  function fbRecLog(kind, data) {
    try {
      var rec = window.HP_FEEDBACK;
      if (rec && typeof rec.log === "function") rec.log(kind, data);
    } catch (e) {}
  }
  function fbRecSetSlide(id) {
    try {
      var rec = window.HP_FEEDBACK;
      if (rec && typeof rec.setCurrentSlide === "function") rec.setCurrentSlide(id);
    } catch (e) {}
  }

  // 当前登录用户角色（/api/auth/info 缓存）。currentRole/currentUserId 是
  // effective subject（预览中为被预览用户）；actorRole 永远是真实管理员。
  var currentRole = null;
  var currentUserId = null;
  // 身份三态（独立复核 #5）：ok（已认证：auth_enabled=true 且有 user_id）/
  // local（服务端明确免认证：auth_enabled=false）/ failed（网络错误、5xx、
  // 响应缺字段、auth_enabled=true 却无 user_id 等）。**只有 ok/local 算
  // 「身份落定」**（authInfoReadyP resolve）；failed 绝不落定——面板的归属
  // 写入（intent.account / upload.account / rebind）与依赖它们的转换启动/
  // 上传全部等待，自动退避重试 + 行上手动「重试」入口，落定后
  // recheckOwnership 重判、等待中的条目继续。401 由 apiFetch 跳 /login。
  // 除面板归属相关路径外，工作台其他功能不等待本信号。
  var authInfoEnabled = false;   // 服务端明确启用认证（auth_enabled=true）
  var authInfoReadyResolve = null;
  var authInfoReadyP = new Promise(function (resolve) {
    authInfoReadyResolve = resolve;
  });
  function authInfoReady() { return authInfoReadyP; }
  function markAuthInfoSettled() {
    if (authInfoReadyResolve) {
      authInfoReadyResolve(null);
      authInfoReadyResolve = null;
    }
    // 身份落定（或变化）→ 编排器重判恢复行/停车行的归属。编排器尚未
    // 初始化时无需补调：ensureInlineOrch 的 restore 同样等这个信号。
    // 微任务延迟：让编排器侧 identityReady 的等待回调先把 identitySettled
    // 翻转，recheckOwnership 才能正确处理各类等待行。
    Promise.resolve().then(function () {
      if (inlineOrch && typeof inlineOrch.recheckOwnership === "function") {
        inlineOrch.recheckOwnership();
      }
    });
  }

  // —— 失败退避重试（复核 #5）：2s/5s/10s/20s/30s 封顶；页面隐藏时暂停 ——
  var AUTH_RETRY_DELAYS_MS = [2000, 5000, 10000, 20000, 30000];
  var authRetryTimer = null;
  var authRetryAttempt = 0;
  function clearAuthRetry() {
    if (authRetryTimer) { clearTimeout(authRetryTimer); authRetryTimer = null; }
    authRetryAttempt = 0;
  }
  function retryAuthInfoNow() {
    // 手动「重试」入口（面板 identity_wait 行）：立即重拉并重置退避
    clearAuthRetry();
    fetchAuthInfo();
  }
  function scheduleAuthRetry() {
    if (authRetryTimer) return;
    var delay = AUTH_RETRY_DELAYS_MS[
      Math.min(authRetryAttempt, AUTH_RETRY_DELAYS_MS.length - 1)];
    authRetryAttempt += 1;
    authRetryTimer = setTimeout(function () {
      authRetryTimer = null;
      if (typeof document !== "undefined" && document.visibilityState &&
          document.visibilityState !== "visible") {
        // 页面隐藏：暂停重试，回到可见再继续（退避档位保留）
        document.addEventListener("visibilitychange", function onVisible() {
          document.removeEventListener("visibilitychange", onVisible);
          if (document.visibilityState === "visible" && !authRetryTimer) {
            scheduleAuthRetry();
          }
        });
        return;
      }
      fetchAuthInfo();
    }, delay);
  }
  /// /api/auth/info 响应三态判定（复核 #5；服务端契约 app.py:7628：顶层
  /// auth_enabled 恒为布尔；auth_enabled=true 时顶层 user_id 为登录用户
  /// id）。缺字段 / auth_enabled=true 却无 user_id 一律 failed。
  function classifyAuthInfo(info) {
    if (!info || typeof info !== "object") return "failed";
    if (info.auth_enabled === false) return "local";   // 服务端明确免认证
    if (info.auth_enabled !== true) return "failed";
    if (!info.user_id) return "failed";
    return "ok";
  }
  function fetchAuthInfo() {
    apiFetch("/api/auth/info").then(function (r) {
      // 401(auth_required) 已在 apiFetch 内跳 /login（页面随即离开，无重试
      // 循环）；其余非 2xx = 身份获取失败
      if (!r || !r.ok) {
        throw new Error("auth/info HTTP " + ((r && r.status) || "?"));
      }
      return r.json();
    }).then(function (info) {
      if (classifyAuthInfo(info) === "failed") {
        // 响应形态异常：绝不落定身份闸（面板绝不写空归属），退避重试。
        // 反馈身份照常落定（与旧链路同：失败路径也不阻塞反馈模块）。
        fbIdentitySettle();
        scheduleAuthRetry();
        return;
      }
      clearAuthRetry();
      applyAuthInfo(info);
      if (sidebarCtrl) sidebarCtrl.onScopeReady();
      markAuthInfoSettled();
      fbIdentitySettle();
    }).catch(function () {
      // 网络错误 / JSON 解析失败：不落定身份闸，退避重试（不影响主功能）；
      // 反馈身份照常落定（同上）
      fbIdentitySettle();
      scheduleAuthRetry();
    });
  }

  var actorRole = null;
  var actorUserId = null;
  var previewState = null;

  function applyAuthInfo(info) {
    // 复核 #5：服务端是否明确启用认证（面板「空归属记录 = 归属未知」哨兵
    // 只在启用认证的部署生效；免认证部署空归属即无归属）
    authInfoEnabled = !!(info && info.auth_enabled);
    if (!info || !info.auth_enabled) return info;
    var previousScope = userScope();
    var actor = info.actor || {};
    previewState = info.preview || null;
    currentRole = info.role || null;
    currentUserId = info.user_id || null;
    actorRole = actor.role || info.role || null;
    actorUserId = actor.user_id || info.user_id || null;
    // Dock initializes before /api/auth/info resolves; restore the authenticated
    // user preference as soon as identity becomes known.
    if (aiPanelCtrl && previousScope !== userScope()) aiPanelCtrl.onBreakpointChange();
    var actorName = actor.username || info.username;
    if (els.logoutBtn) {
      var label = t("toast.logout");
      if (actorName) { label += " (" + actorName + ")"; }
      // Keep the compact label/icon; full identity stays in the tooltip.
      els.logoutBtn.title = label;
      els.logoutBtn.setAttribute("aria-label", label);
      els.logoutBtn.hidden = !!previewState;
    }
    if (els.changepwBtn) { els.changepwBtn.hidden = !!previewState; }
    // 更换邮箱入口与改密/登出同级：预览态隐藏（未登录时 auth_enabled=false
    // 提前返回，入口保持模板里的 hidden，不会出现）
    if (els.changeemailBtn) { els.changeemailBtn.hidden = !!previewState; }
    // 数据共享入口与改密/改绑同级：预览态隐藏（服务端 actor 解析同样拒绝
    // 预览态变更用户授权）；未登录时 auth_enabled=false 提前返回保持 hidden
    if (els.datashareBtn) { els.datashareBtn.hidden = !!previewState; }
    // 反馈问题（改版第四轮 §3）：登录后可见（demo 壳不渲染入口；未登录时
    // auth_enabled=false 在本函数开头提前返回，模板初始 hidden 保持）。
    // 预览态同样可用（反馈随真实会话上送，服务端记录 actor）。
    if (els.feedbackBtn) { els.feedbackBtn.hidden = !info.username; }
    if (els.acctFeedbackBtn) { els.acctFeedbackBtn.hidden = !info.username; }
    // 管理台入口按真实 actor 判定（预览态隐藏——与改密/登出同级约定；
    // 预览中 /admin 仍可手动直达，宿主每条消息回查真实 owner）。
    if (els.adminEntryLink) {
      els.adminEntryLink.hidden = !!previewState || actorRole !== "owner";
    }
    // 账户 chip（§3.5）：视觉文字 = 规范化邮箱 @ 前的 local-part（非邮箱存量
    // 账号回退完整 login_id）；权威身份仍是完整邮箱，popover 展示完整邮箱。
    // 未登录（username 空）不显示；预览态显示被预览 subject 并在 popover
    // 顶部标「管理员预览」，真实 actor 的账户设置入口照旧隐藏。
    if (els.acctBtn) {
      var chipName = acctDisplayUsername(info.username);
      if (els.acctBtnName) els.acctBtnName.textContent = chipName;
      els.acctBtn.hidden = !chipName;
      if (els.acctPopPreview) els.acctPopPreview.hidden = !previewState;
      if (els.acctPopEmail) els.acctPopEmail.textContent = info.username || "";
      if (els.acctPopRole) {
        els.acctPopRole.textContent =
          t(currentRole === "owner" ? "acct.role.owner" : "acct.role.user");
      }
      if (els.acctSettingsBtn) {
        els.acctSettingsBtn.hidden = !!previewState || !info.username;
      }
    }
    if (window.HP_I18N && window.HP_I18N.setRole) { window.HP_I18N.setRole(currentRole); }
    if (els.sharePermHint) {
      els.sharePermHint.hidden = currentRole !== "user";
    }
    applyPreviewBanner(info);
    // P3：预览态不是用户本人操作——停研究采集（服务端写闸亦 403 兜底）
    if (previewState && researchTelemetry) researchTelemetry.stop();
    if (currentRole === "owner") loadDemoCatalog();
    return info;
  }

  function applyPreviewBanner(info) {
    var banner = els.previewBanner;
    if (!banner) return;
    var pv = (info && info.preview) || null;
    if (!pv) {
      banner.hidden = true;
      return;
    }
    var mins = Math.max(1, Math.round(((pv.expires_at || 0) * 1000 - Date.now()) / 60000));
    if (els.previewBannerText) {
      els.previewBannerText.textContent = t("preview.banner", {
        user: pv.subject_username || pv.subject_user_id || "",
        role: pv.subject_role || "",
        mins: mins,
      });
    }
    banner.hidden = false;
  }

  function startIdentityPreview(uid) {
    apiFetch("/api/admin/preview/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: uid }),
    }).then(function (r) {
      return r.json().then(function (b) { return { status: r.status, body: b }; });
    }).then(function (res) {
      if (res.status !== 200) { toast(res.body.error || "预览失败", "error"); return; }
      location.reload();
    }).catch(function (e) {
      toast((e && e.message) ? e.message : "预览失败", "error");
    });
  }

  function stopIdentityPreview() {
    apiFetch("/api/admin/preview/stop", { method: "POST" }).then(function (r) {
      return r.json().then(function (b) { return { status: r.status, body: b }; });
    }).then(function (res) {
      if (res.status !== 200) { toast(res.body.error || "退出预览失败", "error"); return; }
      location.reload();
    }).catch(function (e) {
      toast((e && e.message) ? e.message : "退出预览失败", "error");
    });
  }

  // 页面初始化时拉取认证状态：启用认证则显示「修改我的密码」与退出登录。
  // folder-experience §2：无论成功与否都放行身份就绪闸——位置记忆的恢复/
  // 持久化以 currentUserId 为准（无可信身份 = 不持久化，见 fbMemoryKey）。
  function initAuth() {
    // 复核 #5：/api/auth/info 的拉取/三态判定/退避重试都在 fetchAuthInfo。
    // 成功（ok/local）才落定身份闸；失败不落定、退避重试 + 行上手动重试，
    // 不影响工作台其他功能。
    fetchAuthInfo();
    if (els.previewStopBtn) {
      els.previewStopBtn.addEventListener("click", stopIdentityPreview);
    }
  }

  // 退出登录：POST /logout + CSRF（docs §10.14；GET /logout 入口已随
  // r3-wave1 物理删除——仅存 POST，无兼容期）
  // 产品语义：只有服务端确认退出成功才跳登录页；网络/HTTP 失败留在当前页并提示。
  function doLogout() {
    // P3：登出即停研究采集、清内存缓冲（旧数据不贴到新授权，§7.3）
    if (researchTelemetry) researchTelemetry.stop();
    apiFetch("/logout", { method: "POST" }).then(function (resp) {
      if (!resp || !resp.ok) {
        throw new Error((resp && resp.status) ? ("HTTP " + resp.status) : "logout failed");
      }
      location.href = "/login";
    }).catch(function (e) {
      toast(t("toast.logout.fail", { e: (e && e.message) ? e.message : e }), "error");
    });
  }
  // apiFetch 一并导出：tests/js/api-fetch-401.test.ts 对 401→/login?next=...
  // 跳转契约做行为测试（与 doLogout 同一挂载点）。
  window.HP_AUTH = {
    doLogout: doLogout,
    apiFetch: apiFetch,
    applyAuthInfo: applyAuthInfo,
    startIdentityPreview: startIdentityPreview,
    stopIdentityPreview: stopIdentityPreview,
    // 更换邮箱提交（tests/js/account-email-change.test.ts 行为契约挂载点，
    // 与 apiFetch/applyAuthInfo 同级；init 在 bindEvents 里照常接线）
    changeemailSubmit: changeemailSubmit,
    // 账户 chip / 余额展示（升级 Review 2026-09-09 §3.5）纯函数挂载点
    // （tests/js/toolbar-account-upgrade.test.ts 锁 local-part 回退与
    // nano-CNY 精确换算锚点）
    acctDisplayUsername: acctDisplayUsername,
    acctCny: acctCny,
    acctCredits: acctCreditsUnit,
    // 可用余额整数显示（tests/js/toolbar-account-upgrade.test.ts /
    // credits-page.test.ts 锁向下取整与「不足 1」文案）
    formatCreditsWhole: formatCreditsWhole,
  };

  // ---------- 修改我的密码（账户系统批次 A docs §7.1；owner/user 通用） ----------
  // 弹窗三字段（当前/新/确认，minlength=15 maxlength=200 由模板约束）；
  // POST /api/account/password（JSON + 现有 CSRF header 机制）；成功后服务端
  // 已清空全部 session，前端跳 /login?password_changed=1 重新登录。
  function changepwShowError(msg) {
    if (!els.changepwError) return;
    els.changepwError.textContent = msg || "";
    els.changepwError.hidden = !msg;
  }

  function changepwOpen() {
    if (!els.changepwMask) return;
    closeSidebarDrawerUnderOverlay();
    changepwShowError("");
    els.changepwCurrent.value = "";
    els.changepwNew.value = "";
    els.changepwConfirm.value = "";
    els.changepwMask.style.display = "";
    if (els.changepwCurrent.focus) { setTimeout(function () { els.changepwCurrent.focus(); }, 30); }
  }

  function changepwClose() {
    if (!els.changepwMask) return;
    els.changepwMask.style.display = "none";
  }

  function changepwSubmit() {
    var cur = els.changepwCurrent.value || "";
    var np = els.changepwNew.value || "";
    var cf = els.changepwConfirm.value || "";
    if (!cur || !np || !cf) {
      changepwShowError(tt("acct.changepw.err.required"));
      return;
    }
    if (np !== cf) {
      changepwShowError(tt("acct.changepw.err.mismatch"));
      return;
    }
    var btn = els.changepwSubmitBtn;
    if (btn) { btn.disabled = true; }
    apiFetch("/api/account/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: cur, new_password: np }),
    }).then(function (r) {
      return r.json().then(function (b) { return { status: r.status, body: b, retry: r.headers.get("Retry-After") }; });
    }).then(function (res) {
      if (btn) { btn.disabled = false; }
      if (res.status === 200) {
        // 成功：全部 session 已失效（含本设备），跳登录页带提示参数
        location.href = "/login?password_changed=1";
        return;
      }
      var b = res.body || {};
      if (b.error === "invalid_current_password") {
        changepwShowError(tt("acct.changepw.err.current"));
        return;
      }
      if (res.status === 429) {
        changepwShowError(tt("acct.changepw.err.locked"));
        return;
      }
      if (b.error === "新密码不能与当前密码相同") {
        changepwShowError(tt("acct.changepw.err.same"));
        return;
      }
      // 其余（长度策略等）：直接展示服务端文案（与 store 口径一致）
      changepwShowError(b.error || tt("acct.changepw.err.generic"));
    }).catch(function () {
      if (btn) { btn.disabled = false; }
      changepwShowError(tt("acct.changepw.err.generic"));
    });
  }

  function initChangePw() {
    if (!els.changepwMask) return;
    els.changepwBtn.addEventListener("click", changepwOpen);
    els.changepwClose.addEventListener("click", changepwClose);
    els.changepwCancel.addEventListener("click", changepwClose);
    els.changepwMask.addEventListener("click", function (e) {
      if (e.target === els.changepwMask) { changepwClose(); }
    });
    els.changepwSubmitBtn.addEventListener("click", changepwSubmit);
    els.changepwConfirm.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { changepwSubmit(); }
    });
  }

  // ---------- 更换邮箱（P1-3 身份收口；review-2026-09-08 P2-2 前端闭环） ----------
  // 与改密同一账户设置区、同一弹窗骨架。POST /api/account/email/change/start
  // （JSON + apiFetch 统一 CSRF 头）；成功响应只含掩码新邮箱——明文 token 只经
  // 确认邮件链接送达，绝不回传。用户在 /verify-email-change 确认后服务端才改
  // 绑并 auth_version+1 全端下线（重新登录由确认页文案负责，本页不处理）。
  function changeemailShowError(msg) {
    if (!els.changeemailError) return;
    els.changeemailError.textContent = msg || "";
    els.changeemailError.hidden = !msg;
  }

  function changeemailOpen() {
    if (!els.changeemailMask) return;
    closeSidebarDrawerUnderOverlay();
    changeemailShowError("");
    els.changeemailNew.value = "";
    els.changeemailMask.style.display = "";
    if (els.changeemailNew.focus) { setTimeout(function () { els.changeemailNew.focus(); }, 30); }
  }

  function changeemailClose() {
    if (!els.changeemailMask) return;
    els.changeemailMask.style.display = "none";
  }

  function changeemailSubmit() {
    var ne = (els.changeemailNew.value || "").trim();
    if (!ne) {
      changeemailShowError(t("acct.changeemail.err.required"));
      return;
    }
    var btn = els.changeemailSubmitBtn;
    // 防重复：在途（按钮禁用）时直接忽略后续触发（DOM 禁用之外的 JS 层兜底）
    if (btn && btn.disabled) { return; }
    if (btn) { btn.disabled = true; }
    apiFetch("/api/account/email/change/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_email: ne }),
    }).then(function (r) {
      return r.json().then(function (b) { return { status: r.status, body: b }; });
    }).then(function (res) {
      // 到达终态（成功/失败）一律恢复按钮；成功分支随后关弹窗
      if (btn) { btn.disabled = false; }
      if (res.status === 200) {
        // 成功：确认邮件已入队（掩码邮箱回显；登录态此刻仍有效，不跳转）
        changeemailClose();
        toast(t("acct.changeemail.ok", { email: (res.body && res.body.email_masked) || "" }), "info");
        return;
      }
      // 按服务端机器码映射可读文案（与上传修复 U1 同口径：机器码不原样透出；
      // 未知码/缺码走本地化兜底，不透出服务端原始报文）
      var code = res.body && res.body.code;
      if (code === "email_taken") {
        changeemailShowError(t("acct.changeemail.err.taken"));
      } else if (code === "rate_limited") {
        changeemailShowError(t("acct.changeemail.err.locked"));
      } else if (code === "email_channel_unavailable") {
        changeemailShowError(t("acct.changeemail.err.channel"));
      } else if (code === "invalid_request") {
        changeemailShowError(t("acct.changeemail.err.invalid"));
      } else {
        changeemailShowError(t("acct.changeemail.err.generic"));
      }
    }).catch(function () {
      if (btn) { btn.disabled = false; }
      changeemailShowError(t("acct.changeemail.err.generic"));
    });
  }

  function initChangeEmail() {
    if (!els.changeemailMask) return;
    els.changeemailBtn.addEventListener("click", changeemailOpen);
    els.changeemailClose.addEventListener("click", changeemailClose);
    els.changeemailCancel.addEventListener("click", changeemailClose);
    els.changeemailMask.addEventListener("click", function (e) {
      if (e.target === els.changeemailMask) { changeemailClose(); }
    });
    els.changeemailSubmitBtn.addEventListener("click", changeemailSubmit);
    els.changeemailNew.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { changeemailSubmit(); }
    });
  }

  // ---------- 反馈问题对话框（改版第四轮 §3） ----------
  // 双入口：侧栏底部链接（#feedback-btn）与账户弹层（#acct-feedback-btn）。
  // 描述必填 10–4000 字（实时计数）；「查看将附带的信息」展开即渲染将要
  // POST 的完整 JSON（客户端记录器快照 + 描述原文，所见即所发）。发送经
  // apiFetch（统一 CSRF）：202 → 「已收到，谢谢反馈」后自动关闭；429 →
  // 按 retry_after 显示可重试时间；400/413/其余 → 可读报错。Demo 壳无
  // 本对话框；未登录入口保持模板初始 hidden。
  var FBK_DESC_MIN = 10;
  var FBK_DESC_MAX = 4000;
  var feedbackState = { sending: false, doneTimer: null };

  function feedbackBuildPayload(description) {
    var client = {};
    try {
      var rec = window.HP_FEEDBACK;
      if (rec && typeof rec.snapshot === "function") client = rec.snapshot();
    } catch (e) { client = {}; }
    return { description: description, client: client };
  }

  function feedbackShowError(msg) {
    if (!els.feedbackError) return;
    els.feedbackError.textContent = msg || "";
    els.feedbackError.hidden = !msg;
  }

  function feedbackSetSending(on) {
    feedbackState.sending = !!on;
    if (els.feedbackSubmitBtn) {
      els.feedbackSubmitBtn.disabled = !!on;
      els.feedbackSubmitBtn.textContent = on ? tt("fbk.sending") : tt("fbk.send");
    }
  }

  function feedbackRenderPreview() {
    if (!els.feedbackPreview || !els.feedbackPreviewDetails) return;
    var open = typeof els.feedbackPreviewDetails.open === "boolean"
      ? els.feedbackPreviewDetails.open : true;
    if (!open) { els.feedbackPreview.textContent = ""; return; }
    var desc = (els.feedbackDesc && els.feedbackDesc.value ? els.feedbackDesc.value : "");
    els.feedbackPreview.textContent = JSON.stringify(feedbackBuildPayload(desc), null, 2);
  }

  function feedbackRefreshMeta() {
    var desc = (els.feedbackDesc && els.feedbackDesc.value) || "";
    var shown = desc.length > FBK_DESC_MAX ? desc.slice(0, FBK_DESC_MAX).length : desc.length;
    if (els.feedbackCount) els.feedbackCount.textContent = shown + " / " + FBK_DESC_MAX;
    if (els.feedbackSubmitBtn && !feedbackState.sending) {
      els.feedbackSubmitBtn.disabled = desc.trim().length < FBK_DESC_MIN;
    }
    feedbackRenderPreview();
  }

  function feedbackOpen() {
    if (!els.feedbackMask) return;
    closeSidebarDrawerUnderOverlay();
    if (feedbackState.doneTimer) { clearTimeout(feedbackState.doneTimer); feedbackState.doneTimer = null; }
    feedbackShowError("");
    if (els.feedbackSuccess) els.feedbackSuccess.hidden = true;
    if (els.feedbackDesc) els.feedbackDesc.value = "";
    if (els.feedbackPreviewDetails && "open" in els.feedbackPreviewDetails) {
      els.feedbackPreviewDetails.open = false;
    }
    feedbackSetSending(false);
    els.feedbackMask.style.display = "";
    feedbackRefreshMeta();
    if (els.feedbackDesc && els.feedbackDesc.focus) {
      setTimeout(function () { try { els.feedbackDesc.focus(); } catch (e) {} }, 30);
    }
    // 打开对话框本身也记入操作摘要（帮助还原报错前的操作路径）
    fbRecLog("action", { control: { id: "feedback-dialog", tag: "dialog" } });
  }

  function feedbackClose() {
    if (!els.feedbackMask) return;
    if (feedbackState.doneTimer) { clearTimeout(feedbackState.doneTimer); feedbackState.doneTimer = null; }
    els.feedbackMask.style.display = "none";
  }

  function feedbackSucceeded() {
    if (els.feedbackSuccess) els.feedbackSuccess.hidden = false;
    if (els.feedbackDesc) els.feedbackDesc.value = "";
    feedbackRefreshMeta();
    toast(tt("fbk.success"), "success");
    // 状态可见后再自动关闭（2s）
    feedbackState.doneTimer = setTimeout(function () {
      feedbackState.doneTimer = null;
      feedbackClose();
    }, 2000);
  }

  function feedbackRetryText(retryAfter) {
    var s = Number(retryAfter);
    if (!isFinite(s) || s <= 0) return tt("fbk.err.rate.later");
    if (s < 60) return tt("fbk.err.rate.seconds", { n: Math.ceil(s) });
    return tt("fbk.err.rate.minutes", { n: Math.ceil(s / 60) });
  }

  function feedbackSubmit() {
    if (feedbackState.sending) return;
    var desc = ((els.feedbackDesc && els.feedbackDesc.value) || "").trim();
    if (desc.length < FBK_DESC_MIN) {
      feedbackShowError(tt("fbk.err.short"));
      return;
    }
    if (desc.length > FBK_DESC_MAX) {
      feedbackShowError(tt("fbk.err.long"));
      return;
    }
    feedbackShowError("");
    feedbackSetSending(true);
    apiFetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(feedbackBuildPayload(desc)),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (b) {
        return { status: r.status, body: b || {} };
      });
    }).then(function (res) {
      feedbackSetSending(false);
      if (res.status === 202 || res.status === 200) {
        feedbackSucceeded();
        return;
      }
      if (res.status === 429) {
        feedbackShowError(tt("fbk.err.rate", { time: feedbackRetryText(res.body.retry_after) }));
        return;
      }
      if (res.status === 400) {
        feedbackShowError(tt("fbk.err.short"));
        return;
      }
      if (res.status === 413) {
        feedbackShowError(tt("fbk.err.too_large"));
        return;
      }
      // 401 由 apiFetch 统一跳登录（auth_required）；其余给可读报错
      feedbackShowError(tt("fbk.err.generic", { status: res.status || 0 }));
    }).catch(function () {
      feedbackSetSending(false);
      feedbackShowError(tt("fbk.err.network"));
    });
  }

  function initFeedbackDialog() {
    if (!els.feedbackMask) return;   // demo 壳不渲染
    if (els.feedbackBtn) {
      els.feedbackBtn.addEventListener("click", feedbackOpen);
    }
    if (els.acctFeedbackBtn) {
      els.acctFeedbackBtn.addEventListener("click", function () {
        if (acctPopCtl) acctPopCtl.close();
        feedbackOpen();
      });
    }
    els.feedbackClose.addEventListener("click", feedbackClose);
    els.feedbackCancel.addEventListener("click", feedbackClose);
    els.feedbackMask.addEventListener("click", function (e) {
      if (e.target === els.feedbackMask) { feedbackClose(); }
    });
    els.feedbackSubmitBtn.addEventListener("click", feedbackSubmit);
    els.feedbackDesc.addEventListener("input", feedbackRefreshMeta);
    // 展开即渲染（并随输入刷新；见 feedbackRefreshMeta → feedbackRenderPreview）
    els.feedbackPreviewDetails.addEventListener("toggle", feedbackRenderPreview);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && els.feedbackMask.style.display !== "none") {
        feedbackClose();
      }
    });
  }

  // ---------- 数据共享（P2 账户设置 §3.5：自愿研究授权） ----------
  // 服务端是唯一权威：GET /api/account/agreements（no-store）读当前状态，
  // PUT /api/account/research-consent 带 expected_epoch CAS 提交（409
  // epoch_conflict = 另一标签页先改过，自动重载后由用户重试，不盲覆盖），
  // POST /api/account/research-data/deletion 幂等申请删除研究副本（不删
  // 业务切片/标注/会话）。勾选默认不预选；协议链接只读不改选中状态。
  var datashareState = null;   // 最近一次 GET 的 research 视图（含 epoch）

  function datashareShowError(msg) {
    if (!els.datashareError) return;
    els.datashareError.textContent = msg || "";
    els.datashareError.hidden = !msg;
  }

  function datashareFmtTime(v) {
    if (!v) return "—";
    try {
      var d = new Date(v);
      if (isNaN(d.getTime())) return v;
      return d.toLocaleString();
    } catch (e) { return v; }
  }

  function datashareRender(body) {
    var research = (body && body.research) || {};
    datashareState = research;
    var docs = (body && body.documents) || [];
    var rsDoc = null;
    for (var di = 0; di < docs.length; di++) {
      if (docs[di].document_type === "research_sharing") { rsDoc = docs[di]; break; }
    }
    // grant 版本只取服务端下发的**当前 published** 文稿（draft 服务端必拒绝）
    datashareState._rs_doc_version =
      (rsDoc && rsDoc.status === "published" && rsDoc.version) || null;
    var statusKey;
    if (research.state === "granted") statusKey = "acct.datashare.state.granted";
    else if (research.state === "withdrawn") statusKey = "acct.datashare.state.withdrawn";
    else if (research.state === "declined") statusKey = "acct.datashare.state.declined";
    else statusKey = "acct.datashare.state.none";
    if (els.datashareStatus) {
      els.datashareStatus.textContent = t(statusKey) +
        (body.collection_enabled ? "" : "　·　" + t("acct.datashare.collect.off"));
    }
    if (els.datashareDoc) {
      var parts = [];
      if (rsDoc) {
        parts.push(t("acct.datashare.doc", { version: rsDoc.version || "—" }) +
          "（" + (rsDoc.status === "published" ? t("acct.datashare.doc.published")
                                             : t("acct.datashare.doc.draft")) + "）");
      }
      if (research.state === "granted") {
        parts.push(t("acct.datashare.grantedat", { time: datashareFmtTime(research.granted_at) }));
      } else if (research.state === "withdrawn") {
        parts.push(t("acct.datashare.withdrawnat", { time: datashareFmtTime(research.withdrawn_at) }));
      }
      els.datashareDoc.textContent = parts.join("　·　");
    }
    if (els.datashareScope) {
      els.datashareScope.textContent = t("acct.datashare.scope");
    }
    var legacy = research.legacy_test_application;
    if (els.datashareLegacy) {
      if (legacy && legacy.historical_only) {
        els.datashareLegacy.textContent = t("acct.datashare.legacy",
          { value: legacy.share_research_data ? t("acct.datashare.legacy.true")
                                               : t("acct.datashare.legacy.false") });
        els.datashareLegacy.hidden = false;
      } else {
        els.datashareLegacy.hidden = true;
      }
    }
    var job = research.active_deletion_job;
    if (els.datashareJob) {
      if (job) {
        els.datashareJob.textContent = t("acct.datashare.job", {
          status: job.status || "pending",
          time: datashareFmtTime(job.created_at),
        });
        els.datashareJob.hidden = false;
      } else {
        els.datashareJob.hidden = true;
      }
    }
    if (els.datashareCheck) {
      // 渲染当前选择（服务端权威），但保持「不预选自愿项」：仅 granted 时勾选
      els.datashareCheck.checked = research.state === "granted";
    }
    if (els.datashareWithdraw) {
      els.datashareWithdraw.hidden = research.state !== "granted";
    }
    if (els.datashareSubmit) {
      // 草稿文稿未发布时不能同意（服务端也会拒绝），提交按钮仅控制勾选变化
      els.datashareSubmit.disabled = false;
    }
  }

  function datashareLoad() {
    if (els.datashareStatus) {
      els.datashareStatus.textContent = t("acct.datashare.loading");
    }
    apiFetch("/api/account/agreements").then(function (r) {
      return r.json().then(function (b) { return { status: r.status, body: b }; });
    }).then(function (res) {
      if (res.status !== 200) {
        datashareShowError((res.body && res.body.error) || t("acct.datashare.err.load"));
        return;
      }
      datashareShowError("");
      datashareRender(res.body);
    }).catch(function () {
      datashareShowError(t("acct.datashare.err.load"));
    });
  }

  function datashareOpen() {
    if (!els.datashareMask) return;
    closeSidebarDrawerUnderOverlay();
    datashareShowError("");
    datashareState = null;
    els.datashareMask.style.display = "";
    datashareLoad();
  }

  function datashareClose() {
    if (!els.datashareMask) return;
    els.datashareMask.style.display = "none";
  }

  // 统一 PUT：409 epoch_conflict（多标签页/旧页面）→ 自动重载状态并提示，
  // 绝不拿旧 epoch 盲目重试覆盖他人操作
  function datasharePut(enabled, btn) {
    var payload = { enabled: enabled, expected_epoch: datashareState ? datashareState.epoch : 0 };
    if (enabled) {
      // 版本必填：从服务端下发的当前 published 文稿取（draft 服务端必拒绝）
      var ver = datashareState && datashareState._rs_doc_version;
      payload.document_version = ver || null;
      if (!payload.document_version) {
        datashareShowError(t("acct.datashare.err.nodoc"));
        datashareLoad();
        return;
      }
    }
    if (btn && btn.disabled) { return; }
    if (btn) { btn.disabled = true; }
    apiFetch("/api/account/research-consent", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (r) {
      return r.json().then(function (b) { return { status: r.status, body: b }; });
    }).then(function (res) {
      if (btn) { btn.disabled = false; }
      if (res.status === 409 && res.body && res.body.code === "epoch_conflict") {
        datashareShowError(t("acct.datashare.err.epoch"));
        datashareLoad();
        return;
      }
      if (res.status !== 200) {
        datashareShowError((res.body && res.body.error) || t("acct.datashare.err.save"));
        return;
      }
      datashareShowError("");
      if (enabled) { toast(t("acct.datashare.ok.granted"), "info"); }
      else { toast(t("acct.datashare.ok.withdrawn"), "info"); }
      datashareLoad();
    }).catch(function () {
      if (btn) { btn.disabled = false; }
      datashareShowError(t("acct.datashare.err.save"));
    });
  }

  function datashareSubmit() {
    var want = !!(els.datashareCheck && els.datashareCheck.checked);
    var was = !!(datashareState && datashareState.state === "granted");
    if (want === was) { datashareClose(); return; }
    if (want) {
      var ver = datashareState && datashareState._rs_doc_version;
      if (!ver) {
        datashareShowError(t("acct.datashare.err.nodoc"));
        datashareLoad();
        return;
      }
    }
    datasharePut(want, els.datashareSubmit);
  }

  function datashareWithdraw() {
    datasharePut(false, els.datashareWithdraw);
  }

  function datashareDeleteRequest() {
    var btn = els.datashareDelete;
    if (btn && btn.disabled) { return; }
    if (btn) { btn.disabled = true; }
    apiFetch("/api/account/research-data/deletion", { method: "POST" })
      .then(function (r) {
        return r.json().then(function (b) { return { status: r.status, body: b }; });
      }).then(function (res) {
        if (btn) { btn.disabled = false; }
        if (res.status !== 200) {
          datashareShowError((res.body && res.body.error) || t("acct.datashare.err.del"));
          return;
        }
        datashareShowError("");
        toast(t(res.body && res.body.created
          ? "acct.datashare.ok.del" : "acct.datashare.ok.del.dupe"), "info");
        datashareLoad();
      }).catch(function () {
        if (btn) { btn.disabled = false; }
        datashareShowError(t("acct.datashare.err.del"));
      });
  }

  function initDataShare() {
    if (!els.datashareMask) return;
    els.datashareBtn.addEventListener("click", datashareOpen);
    els.datashareClose.addEventListener("click", datashareClose);
    els.datashareCancel.addEventListener("click", datashareClose);
    els.datashareMask.addEventListener("click", function (e) {
      if (e.target === els.datashareMask) { datashareClose(); }
    });
    els.datashareSubmit.addEventListener("click", datashareSubmit);
    els.datashareWithdraw.addEventListener("click", datashareWithdraw);
    if (els.datashareDelete) {
      els.datashareDelete.addEventListener("click", datashareDeleteRequest);
    }
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(function () { /* ignore */ });
    } else {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) {}
      document.body.removeChild(ta);
    }
  }

  // ---------- user max_steps 只读同步（docs §8.3/§9.2，PT-3） ----------
  // AI 配置面板 DOM 在本页（index.html），但加载/保存逻辑归 HistoPilot 插件
  // bundle（config-panel.js）。平台侧只做轻量补充：use_platform 勾选时把步数
  // 输入框置只读并显示平台生效步数；切回自带 API 后恢复可编辑（默认 20）。
  // 服务端注入规则（_build_sidecar_config）才是权威，这里仅是 UI 提示。
  function syncAiMaxStepsInput() {
    var usePlatform = document.getElementById("ai-use-platform");
    var steps = document.getElementById("ai-max-steps");
    if (!usePlatform || !steps) return;
    var platformOn = usePlatform.checked && !usePlatform.disabled;
    if (platformOn) {
      if (!steps.readOnly) {
        // 暂存自带值，切回自带 API 时恢复
        if (steps.value && !steps.dataset.ownSteps) steps.dataset.ownSteps = steps.value;
        steps.readOnly = true;
        steps.title = tt("ai.field.maxsteps.platform.title");
      }
    } else if (steps.readOnly) {
      steps.readOnly = false;
      steps.title = tt("ai.field.maxsteps.title");
      if (steps.dataset.ownSteps) {
        steps.value = steps.dataset.ownSteps;
        delete steps.dataset.ownSteps;
      } else if (!steps.value) {
        steps.value = 20; // 自带 API 默认步数（docs §4.1）
      }
    }
  }

  function initAiMaxStepsSync() {
    document.addEventListener("change", function (e) {
      if (e.target && e.target.id === "ai-use-platform") syncAiMaxStepsInput();
    });
    // 插件 bundle 加载配置后不派发事件（只设 input.value），用低频轮询补一次
    // 初始只读状态；仅在面板存在且状态未同步时动作（幂等、开销可忽略）。
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      var usePlatform = document.getElementById("ai-use-platform");
      var steps = document.getElementById("ai-max-steps");
      if (!usePlatform || !steps || tries > 20) { clearInterval(timer); return; }
      if (usePlatform.checked) {
        syncAiMaxStepsInput();
        clearInterval(timer);
      }
    }, 1000);
  }

  // 缓存：全部切片、全部项目、全部分享
  var allSlides = [];      // [{name,width,height,mpp_x,...}]
  var allProjects = [];    // [{pid,name,note,slides,roi_count,...}]
  var currentAnnotations = null; // 当前切片的标注 {slide, annotations:[{label,count,items}]}
  var annoOverlays = [];   // 兼容旧引用，已不再新增（标注改画到 canvas）
  var annoPanelOpen = false;
  // 标注列表加载失败态（P3 2026-10-19，生产误报复盘：列表接口瞬时失败曾被
  // 渲染成「暂无标注」，与真正的空切片不可分辨）。非 401 失败（网络断开/
  // 5xx/403/非 JSON 体）：annoListError = 当前切片列表从未成功加载（面板显
  // 示显式错误态 + 重试）；annoListStale = 刷新失败但已有上次成功数据（保留
  // 旧行，面板顶部插失效提示条）。401(auth_required) 仍由 apiFetch 统一跳
  // 登录，不进这两个状态。
  var annoListError = null;   // {status:number}；加载成功或换片即清
  var annoListStale = false;  // true = 面板当前展示的是上次成功加载的数据
  // 列表请求世代（P2 复核 2026-10-18）：loadCurrentSlideAnnotations 与
  // refreshCurrentAnnotations 共用同一计数器。每个新的列表请求 +1，换片也
  // +1 作废在途请求；响应落地前（响应到达时、JSON 解析后、catch 内）比对，
  // 世代已旧即整体静默丢弃——切片对象身份判不了「同一切片内」两个请求的
  // 新旧（review 047 #1：初始加载 500 → 重试挂起 → 保存触发刷新已落地 →
  // 放行旧重试，晚到旧 200 把列表回退、晚到旧 500 把已有数据清成错误页）。
  var annoListReqGen = 0;
  // currentAnnotations 归属的切片对象（与数据同写同清）：load 失败时据此
  // 判定已有成功数据是否属于当前切片——属于才保留数据走 stale 提示；
  // 跨切片残留的旧数据不冒充当前切片的数据。
  var currentAnnotationsSlide = null;
  var allSharesCache = null; // 缓存分享列表，供语言切换时重渲

  // ---------- AI 读片助手状态（Stage 2：平台侧仅保留叠加层） ----------
  // AI 运行状态（aiConfig/aiRunning/aiSessionId/activeAiSession/mainAiCtx 等）已随
  // HistoPilot 插件 bundle 整体迁出（plugins/histopilot/ui/）。平台只保留叠加层：插件
  // 通过 HostBridge viewer.highlight 请求写入 aiOverlay，redrawAnnoCanvas 据此绘制。
  var aiOverlay = [];        // canvas 叠加：agent 的 bbox（goto/snapshot），由 HostBridge 写入
  // AI 判读区配色：在 H&E 粉白底上需高对比；外圈深色描边 + 琥珀色主色 + 标签底衬
  var AI_OVERLAY_FILL = "rgba(255, 149, 0, 0.14)";
  var AI_OVERLAY_STROKE = "#FF9500";
  var AI_OVERLAY_HALO = "rgba(0, 0, 0, 0.82)";
  var AI_ANNO_FILL = "rgba(255, 149, 0, 0.16)";   // 进标注库的 AI 标注填充（比人工略浓）
  var AI_ANNO_STROKE_FALLBACK = "rgba(214, 108, 10, 0.95)";   // AI 标注描边（同族橙，可读）

  // ---------- count_cells 计数范围高亮（方形取景 2026-10-14） ----------
  // count_cells 的工具结果（counted_region.bbox）经浏览器 fetch 到达（HP 插件
  // 消费 run/transcript 响应）。平台侧对 AI 路径的响应体做**只读 tap**（tee 一
  // 支扫描，另一支原样交给插件），命中 counted_region.bbox 后在 viewer 上画一
  // 个临时、轻量的高亮方框：不是标注、不持久化、不建库——下一次计数替换、
  // 新 goto（viewer.navigate）清除、新一轮 run 开始清除、点击方框由用户清除。
  // 高亮绑定当前打开切片：openSlideId 随 openSlide 成功/切片关闭维护（review
  // 2026-10-14 修复 3）——counted_region.slide_id 不等于当前切片（或缺失）
  // 一律不画，transcript 刷新/历史会话的跨切片结果不会画到当前切片上。
  var countHighlight = null;   // {x,y,w,h} level-0 像素
  var openSlideId = "";        // 当前打开切片的稳定 slide_id（高亮绑定用）
  var COUNT_FILL = "rgba(0, 122, 255, 0.10)";
  var COUNT_STROKE = "#007AFF";
  var COUNT_HALO = "rgba(0, 0, 0, 0.8)";

  function setCountHighlight(bbox) {
    // counted_region.bbox（level-0 x/y/w/h）→ 画布临时高亮；非法几何忽略
    // （不覆盖旧高亮，避免畸形数据擦掉用户的可见参照）。
    if (!bbox || typeof bbox !== "object") return false;
    var x = Number(bbox.x), y = Number(bbox.y),
        w = Number(bbox.w), h = Number(bbox.h);
    if (!isFinite(x) || !isFinite(y) || !isFinite(w) || !isFinite(h) ||
        w <= 0 || h <= 0 || x < 0 || y < 0) return false;
    countHighlight = { x: x, y: y, w: w, h: h };
    redrawAnnoCanvas();
    try {
      document.dispatchEvent(new CustomEvent("pt:count-highlight", {
        detail: { bbox: countHighlight },
      }));
    } catch (e) { /* 事件总线缺失不致命 */ }
    return true;
  }

  function clearCountHighlight() {
    if (!countHighlight) return false;
    countHighlight = null;
    redrawAnnoCanvas();
    return true;
  }

  function handleCountedRegion(cr) {
    // counted_region → 高亮（review 2026-10-14 修复 3）：slide_id 必须存在
    // 且等于当前打开切片的稳定 ID；缺失（legacy 通道）或属于其他切片
    // （transcript 刷新/历史会话）一律不画。
    if (!cr || typeof cr !== "object") return false;
    var sid = cr.slide_id;
    if (!sid || !openSlideId || String(sid) !== String(openSlideId)) {
      return false;
    }
    return setCountHighlight(cr.bbox);
  }

  // ---- 只读响应扫描（counted_region.bbox 提取；JSON 与 SSE 双形态） ----
  // 工具结果以 JSON（transcript 刷新）或 SSE data 帧（run 流）里的字符串
  // 到达——后者是转义过的 JSON 文本，解析后再走一层 walk。
  function scanCountRegionText(text) {
    if (!text || text.length > 8 * 1024 * 1024 ||
        text.indexOf("counted_region") < 0) return null;
    var parsed = null;
    try { parsed = JSON.parse(text); } catch (e) { parsed = null; }
    if (parsed === null && text.trim().charAt(0) !== "{" &&
        text.trim().charAt(0) !== "[") return null;
    // 取文档序**最后一个**命中：transcript 按时间排列，多次计数时显示最新一次
    var found = null;
    function walk(node, depth) {
      if (depth > 10 || node == null) return;
      var t = typeof node;
      if (t === "string") {
        if (node.length <= 200000 && node.indexOf("counted_region") >= 0) {
          try { walk(JSON.parse(node), depth + 1); } catch (e) { /* 非JSON */ }
        }
        return;
      }
      if (t !== "object") return;
      var cr = node.counted_region;
      if (cr && typeof cr === "object" && cr.bbox &&
          typeof cr.bbox === "object") {
        var x = Number(cr.bbox.x), y = Number(cr.bbox.y),
            w = Number(cr.bbox.w), h = Number(cr.bbox.h);
        if (isFinite(x) && isFinite(y) && isFinite(w) && isFinite(h) &&
            w > 0 && h > 0 && x >= 0 && y >= 0) {
          found = { bbox: { x: x, y: y, w: w, h: h },
                    slide_id: (typeof cr.slide_id === "string"
                      ? cr.slide_id : "") };
          return;
        }
      }
      for (var k in node) {
        if (Object.prototype.hasOwnProperty.call(node, k)) {
          walk(node[k], depth + 1);
        }
      }
    }
    walk(parsed, 0);
    return found;
  }

  // ---- AI 响应流 tap：tee 响应体，扫描支不阻塞、不修改插件消费的原流 ----
  function pumpCountScan(stream, isSSE) {
    var reader;
    try { reader = stream.getReader(); } catch (e) { return; }
    var decoder = null;
    try { decoder = new TextDecoder("utf-8"); } catch (e) { /* 老环境 */ }
    var frameBuf = "";
    function handleText(text) {
      if (!isSSE) {
        var direct = scanCountRegionText(text);
        if (direct) handleCountedRegion(direct);
        return;
      }
      frameBuf += text;
      var parts = frameBuf.split("\n\n");
      frameBuf = parts.pop() || "";
      if (frameBuf.length > 1024 * 1024) frameBuf = "";  // 防御性截断
      for (var i = 0; i < parts.length; i++) {
        var lines = parts[i].split("\n");
        var dataLines = [];
        for (var j = 0; j < lines.length; j++) {
          if (lines[j].slice(0, 5) === "data:") dataLines.push(lines[j].slice(5).trim());
        }
        if (!dataLines.length) continue;
        var counted = scanCountRegionText(dataLines.join("\n"));
        if (counted) handleCountedRegion(counted);
      }
    }
    (function read() {
      reader.read().then(function (chunk) {
        if (chunk.done) return;
        var text;
        try {
          text = decoder ? decoder.decode(chunk.value, { stream: true })
            : String(chunk.value);
        } catch (e) { return; }
        try { handleText(text); } catch (e) { /* 扫描失败不影响主流程 */ }
        read();
      }, function () { /* 流中断：扫描支静默退出 */ });
    })();
  }

  function tapCountResponse(resp) {
    // 只挂 AI 路径（run/continue/ask/branch/SSE/transcript）；响应经
    // new Response(tee 支) 重建，json()/text()/clone() 语义不变。
    var ctype = "";
    try {
      ctype = String((resp.headers && resp.headers.get("content-type")) || "");
    } catch (e) { return resp; }
    var isSSE = ctype.indexOf("text/event-stream") >= 0;
    var isJSON = ctype.indexOf("json") >= 0;
    if ((!isSSE && !isJSON) || !resp.body ||
        typeof resp.body.tee !== "function") return resp;
    var branches;
    try { branches = resp.body.tee(); } catch (e) { return resp; }
    var forCaller;
    try {
      forCaller = new Response(branches[0], { status: resp.status,
        statusText: resp.statusText, headers: resp.headers });
      // Response.url/redirected 是原型只读 getter，重建实例会回落缺省值——
      // 用自有属性遮蔽还原（review 2026-10-14 修复 3；失败仅影响属性回显）。
      try {
        if (resp.url) {
          Object.defineProperty(forCaller, "url",
            { value: String(resp.url), configurable: true });
        }
        Object.defineProperty(forCaller, "redirected",
          { value: !!resp.redirected, configurable: true });
      } catch (e2) { /* 老环境不可定义 */ }
    } catch (e) { return resp; }  // null-body 状态等：放弃扫描，原样返回
    pumpCountScan(branches[1], isSSE);
    return forCaller;
  }

  function installCountHighlightTap() {
    if (installCountHighlightTap.done) return;
    if (typeof window.fetch !== "function") return;
    installCountHighlightTap.done = true;
    var nativeFetch = window.fetch;
    window.fetch = function (input, init) {
      var promise = nativeFetch.apply(this, arguments);
      try {
        var url = "";
        if (typeof input === "string") url = input;
        else if (input && typeof input.url === "string") url = input.url;
        if (url.indexOf("/api/ai/") < 0 &&
            url.indexOf("/api/demo/ai/") < 0) return promise;
        // 新一轮 run/continue/ask/branch 发起：清上一轮的计数范围高亮
        //（本论 count_cells 结果命中时由扫描支重新设置）。
        if (/(^|\/)api\/(demo\/)?ai\/(run|continue|ask|branch)([?#]|$)/.test(url)) {
          try { clearCountHighlight(); } catch (e) { /* 不阻塞请求 */ }
        }
        return promise.then(function (resp) {
          try { return tapCountResponse(resp); }
          catch (e) { return resp; }
        });
      } catch (e) { return promise; }
    };
  }
  // 平台侧测试/调试入口（scanText 是纯函数，Vitest 直接驱动；noteOpenSlide
  // 由 openSlide 成功/切片关闭的生产路径调用，维护高亮的切片绑定）。
  window.PTCountHighlight = {
    show: setCountHighlight,
    clear: clearCountHighlight,
    current: function () { return countHighlight; },
    scanText: scanCountRegionText,
    handle: handleCountedRegion,
    noteOpenSlide: function (slideId) {
      openSlideId = String(slideId || "");
    },
    install: installCountHighlightTap,
  };

  // 编辑模式状态：选中/拖动（管理端所有标注可编辑）
  // editItem：flatItems 中的引用（可改本地几何）；editDrag：拖动会话
  // editing：备注卡的「保存/取消」模式（§6.2 改版后柄显示与拖动只看
  // 选中 + canDragItem，不再以此标志为前提；仅备注卡按钮形态用）
  var editItem = null;
  var editDrag = null;
  var editing = false;

  // 临时选择器状态
  var pickerCtx = { targetPid: null, selected: {}, mode: "add" };

  // 分享创建用的临时切片集（分享选中 / 项目分享）
  var sharePendingSlides = null; // 若非 null，则用此切片集创建分享

  // ---------- DOM ----------
  var viewer = null;
  function $(id) { return document.getElementById(id); }
  var els = {
    zoomIn: $("zoom-in"),
    zoomOut: $("zoom-out"),
    rotateBtn: $("rotate-btn"),
    flipBtn: $("flip-btn"),
    // 升级 C：单一矩形入口 + 紧凑设置区（旧 roi-6/roi-6-5/roi-box-btn 已移除）
    roiRectBtn: $("roi-rect-btn"),
    roiSettings: $("roi-settings"),
    roiSummary: $("roi-summary"),
    roiWInput: $("roi-w-input"),
    roiHInput: $("roi-h-input"),
    roiUnitSelect: $("roi-unit-select"),
    roiLockRatio: $("roi-lock-ratio"),
    roiPresetSelect: $("roi-preset-select"),
    // Wave 3（普通图片兼容）：无物理标尺提示（模板内联元素；demo 模板无此块时为 null）
    roiNoScaleHint: $("roi-no-scale-hint"),
    saveBtn: $("save-btn"),
    saveAnnoBtn: $("save-anno-btn"),
    annoBtn: $("anno-btn"),
    annoAllBtn: $("anno-all-btn"),
    annoArrowBtn: $("anno-arrow-btn"),
    annoFreeBtn: $("anno-free-btn"),
    annoLabelInput: $("anno-label-input"),
    annoMoreBtn: $("anno-more-btn"),
    annoPop: $("anno-pop"),
    annoCanvas: $("anno-canvas"),
    resetBtn: $("reset-btn"),
    mppSetter: $("mpp-setter"),
    mppInput: $("mpp-input"),
    mppSetBtn: $("mpp-set-btn"),
    zoomBadge: $("zoom-badge"),
    headerZoomBadge: $("header-zoom-badge"),
    zoomNative: $("zoom-native"),
    tbbMoreBtn: $("tbb-more-btn"),
    tbbMore: $("tbb-more"),
    tbbMoreAi: $("tbb-more-ai"),
    uploadBtn: $("upload-btn"),
    fileInput: $("file-input"),
    progressWrap: $("progress-wrap"),
    progressBar: $("progress-bar"),
    progressText: $("progress-text"),
    uploadProgressList: $("upload-progress-list"),
    // P2 复核 2026-10-20（问题 3）：抽屉内的上传队列宿主（行容器的「第二个
    // 家」）。抽屉打开时行容器移到这里（与选择文件的同一面板），关闭时移回
    // 侧栏——同一份 DOM/状态，绝不复制第二份任务列表。
    importUploadsBlock: $("import-uploads-block"),
    importUploadHost: $("import-upload-host"),
    // W3/W4/W6（2026-09-14）：统一「导入切片」抽屉 + 新建项目对话框
    importSlidesBtn: $("import-slides-btn"),
    importDrawer: $("import-drawer"),
    importDrawerMask: $("import-drawer-mask"),
    importDrawerClose: $("import-drawer-close"),
    importTabLocal: $("import-tab-local"),
    importTabBaidu: $("import-tab-baidu"),
    importPanelLocal: $("import-panel-local"),
    importPanelBaidu: $("import-panel-baidu"),
    importPickFiles: $("import-pick-files"),
    importPickFolder: $("import-pick-folder"),
    importFolderInput: $("import-folder-input"),
    importDropZone: $("import-drop-zone"),
    importTargetSelect: $("import-target-select"),
    importTargetNewName: $("import-target-new-name"),
    importFormatCatalog: $("import-format-catalog"),
    importTaskList: $("import-task-list"),
    frSampleMax: $("fr-sample-max"),
    frResult: $("fr-result"),
    frRecent: $("fr-recent"),
    baiduCapStatus: $("baidu-cap-status"),
    baiduInputBlock: $("baidu-input-block"),
    baiduShareText: $("baidu-share-text"),
    baiduExtractCode: $("baidu-extract-code"),
    baiduListBtn: $("baidu-list-btn"),
    baiduEnumStatus: $("baidu-enum-status"),
    baiduCandidatesBlock: $("baidu-candidates-block"),
    baiduSearch: $("baidu-search"),
    baiduCandidateList: $("baidu-candidate-list"),
    baiduPrevBtn: $("baidu-prev-btn"),
    baiduNextBtn: $("baidu-next-btn"),
    baiduPageInfo: $("baidu-page-info"),
    baiduSelectionSummary: $("baidu-selection-summary"),
    baiduTargetSelect: $("baidu-target-select"),
    baiduImportBtn: $("baidu-import-btn"),
    baiduImportStatus: $("baidu-import-status"),
    baiduImportItems: $("baidu-import-items"),
    viewerWrap: $("viewer-wrap"),
    dropOverlay: $("drop-overlay"),
    toastContainer: $("toast-container"),
    logoutBtn: $("logout-btn"),
    // 管理工作台入口（仅 owner 可见；PR5 后 /admin 的唯一 UI 入口）
    adminEntryLink: $("admin-entry-link"),
    // 修改我的密码（账户系统批次 A docs §8.1）
    changepwBtn: $("changepw-btn"),
    changepwMask: $("changepw-mask"),
    changepwClose: $("changepw-close"),
    changepwCancel: $("changepw-cancel"),
    changepwSubmitBtn: $("changepw-submit"),
    changepwCurrent: $("changepw-current"),
    changepwNew: $("changepw-new"),
    changepwConfirm: $("changepw-confirm"),
    changepwError: $("changepw-error"),
    // 更换邮箱（P1-3 身份收口 review-2026-09-08 P2-2；弹窗骨架复用 changepw）
    changeemailBtn: $("changeemail-btn"),
    changeemailMask: $("changeemail-mask"),
    changeemailClose: $("changeemail-close"),
    changeemailCancel: $("changeemail-cancel"),
    changeemailSubmitBtn: $("changeemail-submit"),
    changeemailNew: $("changeemail-new"),
    changeemailError: $("changeemail-error"),
    // 数据共享（P2 账户设置 §3.5：自愿研究授权；弹窗骨架复用 changepw）
    datashareBtn: $("datashare-btn"),
    datashareMask: $("datashare-mask"),
    datashareClose: $("datashare-close"),
    datashareCancel: $("datashare-cancel"),
    datashareSubmit: $("datashare-submit"),
    datashareWithdraw: $("datashare-withdraw"),
    datashareDelete: $("datashare-delete"),
    datashareCheck: $("datashare-check"),
    datashareStatus: $("datashare-status"),
    datashareDoc: $("datashare-doc"),
    datashareScope: $("datashare-scope"),
    datashareLegacy: $("datashare-legacy"),
    datashareJob: $("datashare-job"),
    datashareError: $("datashare-error"),
    // 反馈问题（改版第四轮 §3）：侧栏底部 + 账户弹层双入口 + 对话框
    feedbackBtn: $("feedback-btn"),
    acctFeedbackBtn: $("acct-feedback-btn"),
    feedbackMask: $("feedback-mask"),
    feedbackClose: $("feedback-close"),
    feedbackCancel: $("feedback-cancel"),
    feedbackSubmitBtn: $("feedback-submit"),
    feedbackDesc: $("feedback-desc"),
    feedbackCount: $("feedback-count"),
    feedbackPreviewDetails: $("feedback-preview-details"),
    feedbackPreview: $("feedback-preview"),
    feedbackError: $("feedback-error"),
    feedbackSuccess: $("feedback-success"),
    // AI 面板外框拖动/调整大小（改版第四轮 §1.4；demo 面板无这些 id → 空引用）
    aiPanel: $("ai-panel"),
    aiPanelHeader: $("ai-panel-header"),
    aiPanelResize: $("ai-panel-resize"),
    aiResetPosition: $("ai-reset-position"),
    annoAllToggle: $("anno-all-toggle"),
    annoExportBtn: $("anno-export-btn"),
    // 手机端侧栏抽屉
    menuBtn: $("menu-btn"),
    sidebar: $("sidebar"),
    sidebarMask: $("sidebar-mask"),
    // 改版 2026-10-08：顶栏搜索/分享浮层（§5.1/§5.4；Demo 壳不渲染，全部可空）
    tbSearchBtn: $("tb-search-btn"),
    tbSearchPop: $("tb-search-pop"),
    tbSearchArea: $("tb-search-area"),
    tbSearchResults: $("tb-search-results"),
    tbSearchClose: $("tb-search-close"),
    tbShareBtn: $("tb-share-btn"),
    tbSharePop: $("tb-share-pop"),
    tbShareClose: $("tb-share-close"),
    sharePickBtn: $("share-pick-btn"),
    shareTargetLine: $("share-target-line"),
    // 改版 2026-10-08：文件夹/切片浏览器（§5.1–5.3；Demo 壳不渲染，全部可空）
    fbUpBtn: $("fb-up-btn"),
    fbLocation: $("fb-location"),
    fbCount: $("fb-count"),
    fbPlusBtn: $("fb-plus-btn"),
    fbPlusMenu: $("fb-plus-menu"),
    fbFolderBtn: $("fb-folder-btn"),
    fbFolderMenu: $("fb-folder-menu"),
    fbRename: $("fb-rename"),
    fbMove: $("fb-move"),
    fbShareFolder: $("fb-share-folder"),
    fbDelete: $("fb-delete"),
    fbStack: $("fb-stack"),
    fbPager: $("fb-pager"),
    fbPrevBtn: $("fb-prev-btn"),
    fbNextBtn: $("fb-next-btn"),
    fbPageInfo: $("fb-page-info"),
    // 当前切片名画布标签（§5.1）
    canvasSlideLabel: $("canvas-slide-label"),
    viewerEmpty: $("viewer-empty"),
    viewerEmptyPick: $("viewer-empty-pick"),
    viewerEmptyUpload: $("viewer-empty-upload"),
    // 项目
    // 其他格式请求兼容
    formatReqBtn: $("format-req-btn"),
    formatReqForm: $("format-req-form"),
    frExt: $("fr-ext"),
    frMessage: $("fr-message"),
    frManufacturer: $("fr-manufacturer"),
    frSampleUrl: $("fr-sample-url"),
    frSampleAccessCode: $("fr-sample-access-code"),
    frContact: $("fr-contact"),
    frSample: $("fr-sample"),
    frSubmit: $("fr-submit"),
    frCancel: $("fr-cancel"),
    newProjectBtn: $("new-project-btn"),
    // W3：新建项目对话框（替换内联表单；R3 empty/selection 草稿 + R4 提交锁）
    projectCreateMask: $("project-create-mask"),
    projectCreateDialog: $("project-create-dialog"),
    pcdClose: $("pcd-close"),
    pcdTitle: $("pcd-title"),
    pcdName: $("pcd-name"),
    pcdNote: $("pcd-note"),
    pcdSlidesSummary: $("pcd-slides-summary"),
    pcdSlidesList: $("pcd-slides-list"),
    pcdError: $("pcd-error"),
    pcdCancel: $("pcd-cancel"),
    pcdConfirm: $("pcd-confirm"),
    // 改版 2026-10-08：project-list/unfiled 分区被文件夹浏览器替代（els.fb*）
    // 身份预览 banner（S4：预览态提示 + 退出按钮；用户管理 UI 已迁 admin 插件）
    previewBanner: $("preview-banner"),
    previewBannerText: $("preview-banner-text"),
    previewStopBtn: $("preview-stop-btn"),
    // 分享
    shareMgrBody: $("share-mgr-body"),
    shareExpiresSelect: $("share-expires-select"),
    shareExpiresCustom: $("share-expires-custom"),
    shareRoiSizeSelect: $("share-roi-size-select"),
    shareRectPolicySelect: $("share-rect-policy-select"),
    sharePermAnnotate: $("share-perm-annotate"),
    sharePermDownload: $("share-perm-download"),
    sharePermHint: $("share-perm-hint"),
    shareCreateBtn: $("share-create-btn"),
    shareResult: $("share-result"),
    shareResultUrl: $("share-result-url"),
    shareResultCopy: $("share-result-copy"),
    shareList: $("share-list"),
    // 用户管理（owner）
    // 切片选择器
    pickerMask: $("slide-picker-mask"),
    pickerTitleText: $("picker-title-text"),
    pickerClose: $("picker-close"),
    pickerList: $("picker-list"),
    pickerSelectedCount: $("picker-selected-count"),
    pickerConfirm: $("picker-confirm"),
    // 标注面板
    annoPanel: $("anno-panel"),
    annoPanelTitle: $("anno-panel-title"),
    annoPanelClose: $("anno-panel-close"),
    annoPanelList: $("anno-panel-list"),
    // AI 读片助手（Stage 2：仅保留触发按钮；面板 DOM 与逻辑归插件 bundle）
    aiBtn: $("ai-btn"),
    // 多通道通道着色（Batch 4；元素在 _app_shell.html，RGB/flag 关时保持隐藏）
    channelBtn: $("channel-btn"),
    rgbBadge: $("rgb-badge"),
    channelPanelHost: $("channel-panel"),
    // viewer 画质档（image-transport-upgrade；能力存在才显示）
    qualityControl: $("quality-control"),
    // 账户 chip + popover（升级 Review 2026-09-09 §3.5；Demo 壳不渲染，全部可空）
    acctBtn: $("acct-btn"),
    acctBtnName: $("acct-btn-name"),
    acctPop: $("acct-pop"),
    acctPopPreview: $("acct-pop-preview"),
    acctPopEmail: $("acct-pop-email"),
    acctPopRole: $("acct-pop-role"),
    acctPopScope: $("acct-pop-scope"),
    acctPopRemaining: $("acct-pop-remaining"),
    acctPopUnit: $("acct-pop-unit"),
    acctPopUsage: $("acct-pop-usage"),
    acctPopUsageFill: $("acct-pop-usage-fill"),
    acctPopDetail: $("acct-pop-detail"),
    acctPopExtra: $("acct-pop-extra"),
    acctTopupLink: $("acct-topup-link"),
    acctPopStorage: $("acct-pop-storage"),
    acctPopStorageValue: $("acct-pop-storage-value"),
    acctPopStorageBar: $("acct-pop-storage-bar"),
    acctPopStorageFill: $("acct-pop-storage-fill"),
    acctPopStorageDetail: $("acct-pop-storage-detail"),
    acctSettingsBtn: $("acct-settings-btn"),
  };

  var roiBox = null;
  var dragInfo = null;
  // 底图缩略图层：铺在瓦片层后面的模糊预览，慢网下避免瓦片未到区域变白
  var baseThumbEl = null;
  // 背景桥接层（§4.2.1）：切片时上一张「已解码预览」的原 DOM 节点（不复制
  // 画布、不同步读回像素），在新预览 decode 完成前保持画面连续，杜绝黑闪
  var bridgeThumbEl = null;
  // 正在进行的 viewer.open（= openSlideSeq）：OSD 的 viewer.open() 会先同步
  // 触发一次 close 事件（见 openseadragon open→this.close()），该 close 属于
  // 换片流程、不得清掉刚建好的新预览/桥接层；显式清屏（无切片态/到期）先把
  // 本标记置 0，close 处理器才执行清理。
  var slideOpenActive = 0;

  // 多通道通道着色控制器（Batch 4；channel-controls.js 三页面共用，本页只接
  // adapter/权限。RGB 或 flag 关时 handleInfo 返回 legacy，行为与旧版一致）
  var channelCtrl = null;

  // ---------- 工具函数 ----------
  function toast(msg, type) {
    type = type || "info";
    var el = document.createElement("div");
    el.className = "toast " + type;
    el.textContent = msg;
    els.toastContainer.appendChild(el);
    setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, 3000);
  }

  function fmtSize(bytes) {
    if (bytes == null) return "-";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
    if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + " MB";
    return (bytes / 1073741824).toFixed(2) + " GB";
  }

  function mppTagClass(src) { return src || "missing"; }

  function clamp(v, lo, hi) {
    if (hi < lo) hi = lo;
    return Math.max(lo, Math.min(hi, v));
  }

  function esc(s) {
    // 简易转义，用于 innerHTML 注入（标题等用户输入）
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // 标注徽章文本（如 "标记 3 · 2 人"）
  // 工单 B：人数不再按 label 组数计（一个作者多个 label 组会被重复计人）。
  // 按条目收集**唯一作者**：
  //   - 优先条目上的 author_key/author_kind（标注 ACL 批次注入）；
  //     author_kind === "unknown" / 缺 key 不计（不虚构人）；
  //   - 旧数据回落 owner_user_id，再回落 visitor；
  //   - AI 条目（itemIsAi）计标记不计人。
  function annoAuthorKey(item) {
    if (!item || itemIsAi(item)) return null;
    if (item.author_key != null && item.author_key !== "") {
      if (!item.author_kind || item.author_kind === "unknown") return null;
      return item.author_kind + ":" + item.author_key;
    }
    if (item.owner_user_id != null && item.owner_user_id !== "") {
      return "user:" + item.owner_user_id;
    }
    if (item.visitor) return "visitor:" + item.visitor;
    return null;
  }

  function annoBadgeText(ref) {
    if (!allAnnotationsBySlide) return null;
    // by_slide 索引键是名称快照（后端口径：legacy 名/original_filename 快照）；
    // ref（slide_id 或旧后端的名）先解析到名再查（P2 合同 §5.1：入口按 id 定位；
    // P3 起 id_bundle 行 name=None——快照键回落 original_filename）
    var sinfo = findSlideInfo(ref);
    var grps = allAnnotationsBySlide[(sinfo && (sinfo.name || sinfo.original_filename)) || ref];
    if (!grps || grps.length === 0) return null;
    var total = 0;
    var seen = {};
    grps.forEach(function (g) {
      total += g.count || 0;
      (g.items || []).forEach(function (it) {
        var key = annoAuthorKey(it);
        if (key) seen[key] = true;
      });
    });
    var people = Object.keys(seen).length;
    if (people > 0) return t("badge.marks", { n: total, m: people });
    // 无可识别作者（全部匿名/AI）：只显示标记数，不虚构 0 人
    return t("badge.marks.only", { n: total });
  }

  // 文件名中间截断：保留首尾，中间用 … 连接
  function truncateMiddle(s, max) {
    s = String(s == null ? "" : s);
    if (!max || max < 6) max = 18;
    if (s.length <= max) return s;
    var head = Math.ceil((max - 1) / 2);
    var tail = Math.floor((max - 1) / 2);
    return s.slice(0, head) + "…" + s.slice(s.length - tail);
  }

  // 缓存 annotations_by_slide（从 /api/annotations 拉取全量后缓存）
  var allAnnotationsBySlide = null;
  function loadAnnotationsIndex() {
    return apiFetch("/api/annotations")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        allAnnotationsBySlide = data.by_slide || {};
      })
      .catch(function () { allAnnotationsBySlide = {}; });
  }

  // 某切片是否属于任一项目（P2：项目 DTO 带 slide_ids；按 id 或名双查——
  // id_bundle 行/旧后端名数组兼容）
  function isSlideInAnyProject(s) {
    var ref = s && (s.slide_id || s.name);
    if (!ref) return false;
    for (var i = 0; i < allProjects.length; i++) {
      var p = allProjects[i];
      if (s.slide_id && (p.slide_ids || []).indexOf(s.slide_id) >= 0) {
        return true;
      }
      if (p.slides && p.slides.indexOf(s.name) >= 0) {
        return true;
      }
    }
    return false;
  }

  // label -> 颜色（哈希着色）
  function labelColor(label) {
    var s = String(label || "");
    var h = 0;
    for (var i = 0; i < s.length; i++) {
      h = (h * 31 + s.charCodeAt(i)) >>> 0;
    }
    var hue = h % 360;
    return { fill: "hsla(" + hue + ",70%,55%,0.18)", stroke: "hsl(" + hue + ",70%,45%)" };
  }

  // =========================================================================
  // P3 人工读片行为采集装配（docs/agent-plan-20260921-registration-consent-
  // research.md §7.1）：只由本正式工作台显式装配——demo/公开分享/预览不加载
  // research-viewer-telemetry.js 也不初始化；服务端 capabilities.
  // research_collection（全局开关，默认关闭）关闭时零装配零网络请求。采集
  // 模块自持手势归并/observe_pause/批次发送逻辑，本文件只提供装配点与
  // 「业务写入成功后」的标注回调（失败不产事件）。
  // =========================================================================
  var researchTelemetry = null;

  function initResearchTelemetry() {
    if (!window.HP_ResearchTelemetry || !window.HP_ResearchTelemetry.create) return;
    var caps = (window.HP_APP_BOOTSTRAP && window.HP_APP_BOOTSTRAP.capabilities) || {};
    if (!caps.research_collection) return; // 全量关闭：不装配（无网络请求）
    researchTelemetry = window.HP_ResearchTelemetry.create({});
    researchTelemetry.attach(viewer);
  }

  // 切片打开（emitSlideOpened 同一去重口径）：为本切片建研究会话
  function syncResearchTelemetrySlide() {
    if (!researchTelemetry || !state.slide) return;
    if (previewState) return; // 预览态不是用户本人操作（服务端亦 403 兜底）
    researchTelemetry.startSlide({
      // P2 合同 §5：slide_id 随 spec 下发（采集模块建会话时仍按 slide 名提交
      // ——服务端 /api/research/viewing-sessions 白名单尚未收 slide_id 字段，
      // 会话伪名已由服务端按 slide_id 派生（R-11）；后端补字段后切 body 双发）
      slide_id: state.slide.id || null,
      slide: state.slide.name,
      width: state.slide.width,
      height: state.slide.height,
    });
  }

  function updateResearchBusy() {
    if (!researchTelemetry) return;
    researchTelemetry.setBusy("drawing",
      !!(state.drawMode || drawPhase !== "idle"));
    researchTelemetry.setBusy("roi", !!state.roiMode);
    researchTelemetry.setBusy("panel", !!(annoPanelOpen || editing));
  }

  // ---------- 初始化 OpenSeadragon ----------
  function initViewer() {
    if (window.HP_ViewerCore && HP_ViewerCore.create) {
      viewer = HP_ViewerCore.create($("viewer"));
    } else {
      viewer = OpenSeadragon({
        element: $("viewer"),
        showNavigationControl: false,
        // Tiles are already rendered to RGB by the server. Canvas draws them
        // directly, avoiding OSD 5's offscreen WebGL-to-2D copy on every pan frame.
        drawer: "canvas",
        imageLoaderLimit: 8,
        placeholderFillStyle: null,
        compositeOperation: "source-over",
        minZoomImageRatio: 0.5,
        maxZoomPixelRatio: 10,
        minPixelRatio: 0.4,
        defaultZoomLevel: 0,
        immediateRender: false,
        preload: false,
        wrapHorizontal: false,
        wrapVertical: false,
        preserveImageSizeOnResize: true,
        pixelsPerWheelLine: 40,
        gestureSettingsMouse: {
          scrollToZoom: true,
          clickToZoom: false,
          dblClickToZoom: true,
        },
        gestureSettingsTouch: {
          pinchToZoom: true,
          flickEnabled: false,
        },
        animationTime: 0.3,
        visibilityRatio: 0.1,
        prefixUrl: "",
      });
      viewer.container.style.backgroundColor = "#262a30";
    }
    viewer.addHandler("zoom", function () { updateZoomBadge(); syncBaseThumb(); });
    viewer.addHandler("open", onViewerOpen);
    // 打开失败=明确失败（§4.2.7）：飞牌、桥接层与旧预览一并退场，不把旧图
    // 留在屏上冒充新切片
    viewer.addHandler("open-failed", function () {
      slideOpenActive = 0;
      cancelSlideDeal();
      clearBaseThumb();
      clearBridgeThumb();
    });
    // Works for both Canvas and WebGL drawers. An open event only means the
    // tile source is known; do not uncover an empty canvas on a slow connection.
    // 就绪判定（§4.2.5）：不能只凭 e.tiles.length>0——可见图像区域须被本帧
    // 已绘制且完成必要混合的瓦片覆盖；有完整已解码底图时无须等高清瓦片
    //（见 finishSlideDeal / dealHandoffReady）。
    viewer.addHandler("tiled-image-drawn", function (e) {
      if (!slideDeal || !slideDeal.opened || slideDeal.seq !== openSlideSeq ||
          !e.tiles || !e.tiles.length || !viewer.world ||
          e.tiledImage !== viewer.world.getItemAt(0)) return;
      if (tilesCoverVisibleViewport(e.tiledImage, e.tiles)) slideDeal.tilesCovered = true;
      finishSlideDeal(slideDeal);
    });
    // A laid-out preview must never cover a user's attempt to pan the viewer.
    viewer.addHandler("canvas-press", cancelSlideDeal);
    viewer.addHandler("canvas-scroll", cancelSlideDeal);
    // 底图随平移/缩放实时跟随（animation 每帧触发，跟随最平滑）
    viewer.addHandler("animation", function () { syncBaseThumb(); redrawAnnoCanvas(true); });
    // 动画结束补画文本（标签/气泡）：动画期间为流畅省略了文本绘制
    viewer.addHandler("animation-finish", function () { redrawAnnoCanvas(); });
    viewer.addHandler("rotate", function () { syncBaseThumb(); redrawAnnoCanvas(); });
    // 镜像翻转：OSD 'flip' 事件 → 同步底图 transform / 重绘标注画布 / ROI 框重对位
    viewer.addHandler("flip", function () {
      state.flipped = !!viewer.viewport.getFlip();
      applyBaseThumbFlip();
      syncBaseThumb();
      redrawAnnoCanvas();
      updateRoiOverlay();
    });
    // 容器尺寸变化（窗口 resize、侧栏收起/展开、断点切换）：OSD 重算 viewport
    // 后触发；先同步画布背衬尺寸再重绘，保证标注/AI overlay 与新容器对齐（§4.2）
    viewer.addHandler("resize", function () {
      resizeAnnoCanvas();
      redrawAnnoCanvas();
    });
    // 切片关闭时清理旧底图；P3：同时结束研究读片会话、清旧缓冲。
    // 注意 viewer.open() 自己会先同步 close 一次（换片流程）：那一步的旧
    // 预览/桥接层正是新切片的背景连续性（§4.2.1），只有显式清屏
    // （slideOpenActive 已复位为 0 的无切片态/到期/失败）才真正清理。
    viewer.addHandler("close", function () {
      if (slideOpenActive !== openSlideSeq) {
        clearBaseThumb();
        clearBridgeThumb();
      }
      if (researchTelemetry) researchTelemetry.endSlide();
    });
  }

  // slide.opened 发射（id|revision 去重，P2 合同 §5.5；旧后端无 id 回落
  // name|revision）。语义是「插件应按此切片重置/恢复状态」：键相同（同切片同
  // revision，如换配色重开）跳过——不重置插件 AI 会话；键变化（切换切片，或
  // 同名文件替换 → revision 变化）必然重发。
  // 正常路径与 channelReopening 轻量路径都必须走这里：多通道切片带本地持久化
  // 配色时，「首开默认 token → applySelection 同步 viewer.close() + 置位重开」
  // 时序下第一次 open 事件被 close 吃掉、第二次 open 只会从轻量路径到达——
  // 轻量路径若不发，插件停留旧切片（生产 bug：slide.opened 3/3 丢失，会话
  // 恢复/切片替换保护全部失效）；发了则由键去重保证纯换配色不重发、且两次
  // open 事件无论谁先到达都恰好送达一次。
  function emitSlideOpened() {
    if (!state.slide || !(state.slide.id || state.slide.name)) return;
    var key = (state.slide.id || state.slide.name) + "|" + (state.slide.revision || "");
    if (key === state.lastSlideOpenedKey) return;
    state.lastSlideOpenedKey = key;
    hpEmit("slide.opened", { slide: {
      // P2 合同 §5.5：载荷主键 id；name/display_name 为兼容快照（旧插件按
      // name 解析当前切片——退役条件 P6 插件全面切 id）
      id: state.slide.id || null,
      name: state.slide.name,
      display_name: state.slide.display_name || null,
      width: state.slide.width, height: state.slide.height,
      mppX: state.slide.mppX, mppY: state.slide.mppY,
      // 资产 revision（服务端口径 = "mtime_ns:size"）：插件快照回看时比对
      // view.slide_revision，检测切片被替换（宽容缺省 → null，保护退化为不拦截）
      revision: state.slide.revision || null,
    } });
    // P3：为本切片建研究读片会话（采集开关关闭/预览态时为无操作）
    syncResearchTelemetrySlide();
  }

  function onViewerOpen() {
    if (slideDeal && slideDeal.seq === openSlideSeq && state.slide &&
        String(state.slide.id || state.slide.name) === slideDeal.ref) {
      slideDeal.opened = true;
    }
    // AI 助手：切片一旦可用即启用触发按钮（插件停用时 aiBtn 不渲染，跳过；
    // 平台人工读片不受影响）。必须放在 channelReopening 分支之前：模板初始
    // disabled，而多通道切片带本地配色时首开被 close 吃掉、最终 open 只走
    // 轻量路径——若在普通分支才解除禁用，首开该切片时 AI 入口保持灰色。
    if (els.aiBtn) els.aiBtn.disabled = false;
    if (els.tbbMoreAi) els.tbbMoreAi.disabled = false;  // ⋯ 面板里的 AI 钮同步
    // 通道配色重开（同一切片换 TileSource，§8.2）：走轻量路径——只同步倍率
    // 徽章与底图缩略图；不退绘制模式、不重置标注/AI 面板。slide.opened 仍经
    // emitSlideOpened 补发（键相同自动去重，见上），不再无条件吞掉。
    if (state.channelReopening) {
      state.channelReopening = false;
      updateZoomBadge();
      syncBaseThumb();
      maybeRetireBridgeThumb();
      if (slideDeal) spreadSlideDeal(slideDeal);
      emitSlideOpened();
      return;
    }
    updateZoomBadge();
    // 打开后把底图缩略图对齐到当前视口
    syncBaseThumb();
    // viewer 已 open 到本切片：新预览若已 decode，桥接层即可退场（§4.2.3）
    maybeRetireBridgeThumb();
    if (slideDeal) spreadSlideDeal(slideDeal);
    // 打开新切片：退出绘制模式、清面板、重置标注画布尺寸
    exitDrawMode();
    resizeAnnoCanvas();
    els.annoBtn.disabled = true;
    els.annoAllBtn.disabled = true;
    els.annoPanel.style.display = "none";
    annoPanelOpen = false;
    annoListError = null;
    annoListStale = false;
    // 换片作废在途列表请求（load/refresh 共用世代，见 annoListReqGen 声明）；
    // 随后的 loadCurrentSlideAnnotations 会再 +1，此处兜住换片后不发 load 的
    // 分支（如关闭切片），保证在途响应一律判旧。
    annoListReqGen++;
    syncAnnoAllBtns();
    state.showAnno = false;
    state.focusAnno = null;
    // 平台侧清叠加层（切片隔离）。插件侧会话 UI/SSE/游标由 slide.opened event 触发其自行 reset+restore。
    aiOverlay = [];
    countHighlight = null;  // 计数范围高亮随切片关闭一并清除（不跨切片残留）
    // 本回调在 openSlide 已写入 state.slide 之后才触发：绑定取刚打开的切片，
    // 不能清空（否则新切片的计数结果永远不画）
    openSlideId = String((state.slide && state.slide.id) || "");
    redrawAnnoCanvas();
    emitSlideOpened();
    if (state.slide) {
      // 管理员标注工具在任意打开的切片上可用（箭头/描图不依赖 mpp）
      els.annoArrowBtn.disabled = false;
      els.annoFreeBtn.disabled = false;
      // 拉取该切片标注（P2 合同 §3.1：?slide_id= 优先，旧后端 ?slide=<名>；
      // 迟到响应防护与非 401 失败的显式错误态见 loadCurrentSlideAnnotations）
      loadCurrentSlideAnnotations();
    } else {
      els.annoArrowBtn.disabled = true;
      els.annoFreeBtn.disabled = true;
      redrawAnnoCanvas();
    }
  }

  // 把"图像缩放比"换算成读片软件常用的物镜等效倍率（如 20× / 40×）。
  // 约定屏幕 96 DPI（1 屏像素 ≈ 25400/96 µm）；缺 mpp 时无法换算，回退百分比。
  function formatMag(mag) {
    // 全片概览时屏显等效倍率会到天文数字（无物理意义），缩写为 k 避免撑爆徽章
    if (mag >= 1000000) return (mag / 1000000).toFixed(1).replace(/\.0$/, "") + "M×";
    if (mag >= 10000) return Math.round(mag / 1000) + "k×";
    if (mag >= 10) return Math.round(mag) + "×";
    if (mag >= 1) return mag.toFixed(1) + "×";
    return mag.toFixed(2) + "×";
  }
  // AI 轨迹里的倍率：可能是数字（需格式化）或已带单位的字符串（如 "20x (high power)"）
  function fmtAiMag(mag) {
    if (mag === null || mag === undefined || mag === "") return "";
    if (typeof mag === "string") return mag;  // 已格式化（如 ai_agent 的 magnification_label）
    var m = Number(mag);
    if (!isFinite(m)) return String(mag);
    return (m >= 10 ? Math.round(m) : m.toFixed(1)) + "x";
  }
  function updateZoomBadge() {
    var text = (window.HP_ViewerCore && HP_ViewerCore.zoomText)
      ? HP_ViewerCore.zoomText(viewer, state.mppX)
      : "—";
    if (text === "—" && !(window.HP_ViewerCore && HP_ViewerCore.zoomText)) {
      try {
        if (viewer && viewer.viewport && viewer.source) {
          var zoom = viewer.viewport.getZoom(true);
          var containerW = viewer.viewport.getContainerSize().x;
          var imgW = viewer.source.dimensions.x;
          var imageZoom = (zoom * containerW) / imgW;
          var mpp = state.mppX;
          if (mpp && mpp > 0 && imageZoom > 0) {
            text = formatMag(imageZoom * (10 / mpp));
          } else {
            text = Math.round(imageZoom * 100) + "%";
          }
        }
      } catch (e) { /* 保持 — */ }
    }
    if (els.zoomBadge) els.zoomBadge.textContent = text;
    if (els.headerZoomBadge) els.headerZoomBadge.textContent = text;
  }

  // ---------- 底图缩略图层（慢网下瓦片未到区域的模糊预览） ----------
  function applyBaseThumbFlip() {
    if (!baseThumbEl) return;
    baseThumbEl.style.transformOrigin = "center";
    baseThumbEl.style.transform = state.flipped ? "scaleX(-1)" : "";
  }

  function thumbDecoded(el) {
    return !!(el && el.complete && el.naturalWidth > 0);
  }

  // img 的 decode 门控（§4.2.3）：绝不以「src 已赋值」代替
  // complete/naturalWidth 检查；已解码立即兑现，否则等 load/error。
  function whenThumbDecoded(el) {
    return new Promise(function (resolve) {
      if (!el) { resolve(false); return; }
      if (thumbDecoded(el)) { resolve(true); return; }
      var settled = false;
      function done(ok) {
        if (settled) return;
        settled = true;
        el.removeEventListener("load", onLoad);
        el.removeEventListener("error", onError);
        resolve(ok);
      }
      var onLoad = function () { done(true); };
      var onError = function () { done(false); };
      el.addEventListener("load", onLoad);
      el.addEventListener("error", onError);
    });
  }

  function clearBridgeThumb() {
    if (bridgeThumbEl) {
      if (bridgeThumbEl.parentNode) bridgeThumbEl.parentNode.removeChild(bridgeThumbEl);
      bridgeThumbEl = null;
    }
  }

  // 桥接层退场：新预览已解码并在位后才允许（§4.2.3）。淡出只作用在这一个
  // 待退场图层上，不同步降任何别的 alpha（§4.2.6）。
  function retireBridgeThumb() {
    if (!bridgeThumbEl) return;
    var el = bridgeThumbEl;
    bridgeThumbEl = null;
    if (!el.parentNode) return;
    if (typeof el.animate !== "function") { el.parentNode.removeChild(el); return; }
    try {
      var anim = el.animate([{ opacity: 0.9 }, { opacity: 0 }],
        { duration: 180, fill: "forwards" });
      var drop = function () { if (el.parentNode) el.parentNode.removeChild(el); };
      anim.finished.then(drop, drop);
    } catch (e) {
      if (el.parentNode) el.parentNode.removeChild(el);
    }
  }

  // 退场条件：新切片的底图预览已解码，且（无翻牌流程或 viewer 已 open 到
  // 该切片）。两个事件源（预览 decode / viewer open）谁后到谁触发。
  function maybeRetireBridgeThumb() {
    if (!bridgeThumbEl) return;
    var opened = !slideDeal || slideDeal.opened;
    if (thumbDecoded(baseThumbEl) && opened) retireBridgeThumb();
  }

  // 换片前的背景桥接（§4.2.1/§4.2.2）：在 clearBaseThumb/viewer.open 清理
  // 之前，把上一张已解码预览的原 DOM 冻结为桥接层。
  // 优先级 1：翻牌层里已解码的缩略图（此时它才是用户眼前的那张图，冻结
  // 其当前动画位置）；优先级 2：底图预览节点本身（几何已由 syncBaseThumb
  // 维护）。两者都复用现有节点，不 toDataURL、不 getImageData、不复制画布。
  function promoteSlideDealPreviewToBridge() {
    var deal = slideDeal;
    if (!deal || !deal.image || !viewer || !viewer.container) return false;
    var img = deal.image;
    if (!thumbDecoded(img)) return false;
    var landingRect = deal.landing
      ? deal.landing.getBoundingClientRect()
      : (img.parentNode && img.parentNode.getBoundingClientRect ? img.parentNode.getBoundingClientRect() : null);
    var containerRect = viewer.container.getBoundingClientRect();
    if (!landingRect || !landingRect.width || !landingRect.height || !containerRect) return false;
    bridgeThumbEl = img;
    img.className = "osd-base-thumb osd-base-thumb-bridge";
    img.style.left = (landingRect.left - containerRect.left) + "px";
    img.style.top = (landingRect.top - containerRect.top) + "px";
    img.style.width = landingRect.width + "px";
    img.style.height = landingRect.height + "px";
    img.style.transform = "none";
    viewer.container.insertBefore(img, viewer.canvas);
    return true;
  }

  function promoteBaseThumbToBridge() {
    if (!baseThumbEl) return false;
    if (!thumbDecoded(baseThumbEl) || !baseThumbEl.parentNode) {
      // 从未解码的预览不可见，无桥接价值
      if (baseThumbEl.parentNode) baseThumbEl.parentNode.removeChild(baseThumbEl);
      baseThumbEl = null;
      return false;
    }
    bridgeThumbEl = baseThumbEl;
    bridgeThumbEl.classList.add("osd-base-thumb-bridge");
    baseThumbEl = null;   // 几何内联值原样冻结；syncBaseThumb 不再改写它
    return true;
  }

  function promotePreviousPreview() {
    clearBridgeThumb();
    if (!promoteSlideDealPreviewToBridge()) promoteBaseThumbToBridge();
  }

  function clearBaseThumb() {
    if (baseThumbEl) {
      if (baseThumbEl.parentNode) baseThumbEl.parentNode.removeChild(baseThumbEl);
      baseThumbEl = null;
    }
  }

  // 新底图预览 decode 完成（§4.2.3）：对齐几何 → 具备条件即让桥接层退场，
  // 并推动翻牌交接判定。迟到的 decode（seq 已过期）不触碰任何图层。
  function onBaseThumbDecoded(seq) {
    if (seq !== openSlideSeq) return;
    syncBaseThumb();
    maybeRetireBridgeThumb();
    if (slideDeal) finishSlideDeal(slideDeal);
  }

  // 给 baseThumbEl 挂 decode 监听（src 可能在挂监听后才被设置，如通道控制
  // 器 setThumbnail），decode 完成走 onBaseThumbDecoded
  function armBaseThumbDecodeWatch(seq) {
    if (!baseThumbEl) return;
    whenThumbDecoded(baseThumbEl).then(function (ok) {
      if (!ok || seq !== openSlideSeq) return;
      onBaseThumbDecoded(seq);
    });
  }

  function syncBaseThumb() {
    if (!baseThumbEl || !viewer || !viewer.viewport || !state.slide) return;
    var W = state.slide.width, H = state.slide.height;
    if (!W || !H) return;
    try {
      var tl = viewer.viewport.imageToViewerElementCoordinates(new OpenSeadragon.Point(0, 0));
      var br = viewer.viewport.imageToViewerElementCoordinates(new OpenSeadragon.Point(W, H));
      var left = Math.min(tl.x, br.x);
      var top = Math.min(tl.y, br.y);
      var width = Math.abs(br.x - tl.x);
      var height = Math.abs(br.y - tl.y);
      baseThumbEl.style.left = left + "px";
      baseThumbEl.style.top = top + "px";
      baseThumbEl.style.width = width + "px";
      baseThumbEl.style.height = height + "px";
      // 仅当旋转角为 0/180 时显示底图，避免 90/270 错位（瓦片本身正常旋转显示）
      baseThumbEl.style.display = (state.rotation % 180 === 0) ? "block" : "none";
    } catch (e) {}
  }

  // ---------- 多通道通道着色（Batch 4；channel-controls.js 三页面共用） ----------
  // localStorage 用户作用域（§8.3）：登录用户区分（预览中为被预览用户），
  // 本机免登录统一 local
  function userScope() {
    return "official:" + (currentUserId || "local");
  }

  function createChannelController() {
    return HP_Channels.createChannelController({
      // P2：通道读端点按 ID 通道（/api/slides/<id>/tiles|dzi|thumbnail）；
      // render-context 走 by-name（后端缺口，见 channelControlsAdapter 注释）
      adapter: channelControlsAdapter(),
      viewer: viewer,
      button: els.channelBtn,
      badge: els.rgbBadge,
      panelHost: els.channelPanelHost,
      t: t,
      toast: toast,
      storage: window.localStorage,
      // 通道重开（同一切片换配色，§8.2）：onViewerOpen 走轻量路径
      onReopening: function () { state.channelReopening = true; },
      onReopened: function () { syncBaseThumb(); redrawAnnoCanvas(); },
      // render 计划的打开统一由控制器发起（含 409 刷新 info 重建路径）；
      // legacy 计划由 openSlide 走原 DZI 路径
      open: function (plan) {
        if (viewer && plan && plan.tileSource) viewer.open(plan.tileSource);
      },
      // 底图缩略图与屏幕瓦片同 token（§4.4）；viewer.close 清掉后这里重建。
      // 重建后重新挂 decode 门控：decode 完成才让桥接层退场/推动交接
      setThumbnail: function (url) {
        if (!url) return;
        if (!baseThumbEl && viewer && viewer.container) {
          baseThumbEl = document.createElement("img");
          baseThumbEl.className = "osd-base-thumb";
          baseThumbEl.alt = "";
          viewer.container.insertBefore(baseThumbEl, viewer.canvas);
          applyBaseThumbFlip();
        }
        if (baseThumbEl) {
          baseThumbEl.src = url;
          armBaseThumbDecodeWatch(openSlideSeq);
        }
      },
      // 409 slide_revision_conflict：只刷新 info 并重建一次（§6.3）。
      // slide ID 化（P2）：按当前切片操作键（id 优先；旧后端回落 name）刷新
      refreshInfo: function () {
        if (!state.slide) return Promise.resolve(null);
        return apiFetch(slideInfoUrl(state.slide.id || state.slide.name))
          .then(function (r) { return r.json(); });
      },
    });
  }

  // ---------- 文档标题（§3.4：品牌区 Beta 徽标后切片名只进 document.title） ----------
  function updateDocTitle(slideName) {
    var base = t("app.doc.title");
    try {
      document.title = slideName ? (slideName + " · " + base) : base;
    } catch (e) { /* 非 DOM 环境忽略 */ }
  }

  // =========================================================================
  // slide ID 化（P2 合同 §5）：切片操作键 = slide_id。
  // -------------------------------------------------------------------------
  // 能力协商唯一口径（合同 §5.6）：HP_APP_BOOTSTRAP.capabilities.slide_id_api
  // 由服务端注入（本版本起恒 true）；false/缺省 = 旧后端 → 全链回落 name
  // 通道（旧端点 /api/slide/<name>/...）。
  // 退役条件：P6 拆除 name 回落时删除本 helper 与全部调用点的回落分支。
  function slideIdApiOn() {
    try {
      var caps = window.HP_APP_BOOTSTRAP && window.HP_APP_BOOTSTRAP.capabilities;
      return !!(caps && caps.slide_id_api === true);
    } catch (e) { return false; }
  }

  // 列表/行显示名（合同 §5.4）：display_name 优先，空则 original_filename/
  // legacy 名；title 辅助显示 original_filename。
  function slideDisplayName(s) {
    return String((s && (s.display_name || s.original_filename || s.name)) || "");
  }

  // 切片操作键：ID 通道 = slide_id；旧后端（列表 DTO 无 slide_id）回落
  // legacy 文件名——与列表行 data-slide-id 同源（P6 拆除 name 回落）。
  function slideRefOf(s) {
    return String((s && (s.slide_id || s.id)) || (s && s.name) || "");
  }
  function activeSlideRef() {
    return state.slide ? String(state.slide.id || state.slide.name) : "";
  }

  // 读端点 URL 族（ID 通道 /api/slides/<id>/...；旧后端 /api/slide/<name>/...）。
  // demo 通道保留 adapter.slideInfoUrl 兼容分支（demo.js 一直是 slide_id 通道）。
  function slideInfoUrl(ref) {
    var adapter = window.HP_API;
    if (adapter && adapter.slideInfoUrl && adapter.mode === "demo") {
      return adapter.slideInfoUrl(ref);
    }
    return slideIdApiOn()
      ? "/api/slides/" + encodeURIComponent(ref) + "/info"
      : "/api/slide/" + encodeURIComponent(ref) + "/info";
  }
  function slideDziUrl(ref) {
    var adapter = window.HP_API;
    if (adapter && adapter.dziUrl && adapter.mode === "demo") {
      return adapter.dziUrl(ref);
    }
    return slideIdApiOn()
      ? "/api/slides/" + encodeURIComponent(ref) + "/dzi"
      : "/api/slide/" + encodeURIComponent(ref) + ".dzi";
  }
  function slideThumbnailUrl(ref, renderToken, qualityQuery) {
    var adapter = window.HP_API;
    if (adapter && adapter.thumbnailUrl && adapter.mode === "demo") {
      return adapter.thumbnailUrl(ref, renderToken, qualityQuery);
    }
    var base = slideIdApiOn()
      ? "/api/slides/" + encodeURIComponent(ref) + "/thumbnail"
      : "/api/slide/" + encodeURIComponent(ref) + "/thumbnail";
    return base + tileQueryJoin(renderToken, qualityQuery);
  }

  // 瓦片/缩略图 query 拼接（画质前置 ?profile=&dv=、render 后置；与
  // app-mode.js tileUrlQuery 同一顺序，image-transport-upgrade §5.2）
  function tileQueryJoin(renderToken, qualityQuery) {
    var parts = [];
    if (qualityQuery) parts.push(String(qualityQuery).replace(/^\?/, ""));
    if (renderToken) parts.push("render=" + encodeURIComponent(renderToken));
    return parts.length ? "?" + parts.join("&") : "";
  }

  // 通道控制器 adapter（P2 合同 §5.1）：ID 通道读端点 /api/slides/<id>/...
  // （dzi/tiles/thumbnail 族已上线）。render-context 后端暂无 by-id 端点
  // （P2 后端缺口，见交付报告）——按当前切片 legacy 名走旧通道；render
  // token 本就按 slide_id 绑定（R-15），URL 形态不影响 token 有效性。
  // demo 页不经此包装（app.js 只在 official 页加载；demo.js 自带 id 通道）。
  function channelControlsAdapter() {
    var base = window.HP_API || {};
    return {
      mode: base.mode || "official",
      tileUrl: function (id, level, x, y, renderToken, qualityQuery) {
        var seg = slideIdApiOn()
          ? "/api/slides/" + encodeURIComponent(id) + "/tiles/" + level +
            "/" + x + "_" + y + ".jpeg"
          : "/api/slide/" + encodeURIComponent(id) + "_files/" + level +
            "/" + x + "_" + y + ".jpeg";
        return seg + tileQueryJoin(renderToken, qualityQuery);
      },
      thumbnailUrl: function (id, renderToken, qualityQuery) {
        return slideThumbnailUrl(id, renderToken, qualityQuery);
      },
      dziUrl: function (id) {
        return slideDziUrl(id);
      },
      normalizeRenderContext: function (id, body) {
        // by-name 端点（缺口见上）：ID 通道时 id 是 slide_id，换成当前切片名
        var name = (state.slide && state.slide.name) || id;
        if (base.normalizeRenderContext) {
          return base.normalizeRenderContext(name, body);
        }
        return apiFetch("/api/slide/" + encodeURIComponent(name) + "/render-context", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body || {}),
        });
      },
    };
  }
  // 裁剪（升级 C v2 矩形 x/y/w/h；render token 可选）
  function slideCropUrl(ref, r, token) {
    if (slideIdApiOn()) {
      return "/api/slides/" + encodeURIComponent(ref) +
        "/crop?x=" + Math.round(r.x) + "&y=" + Math.round(r.y) +
        "&w=" + Math.round(r.w) + "&h=" + Math.round(r.h) +
        (token ? "&render=" + encodeURIComponent(token) : "");
    }
    return "/api/slide/" + encodeURIComponent(ref) +
      "/crop?x=" + Math.round(r.x) + "&y=" + Math.round(r.y) +
      "&w=" + Math.round(r.w) + "&h=" + Math.round(r.h) +
      (token ? "&render=" + encodeURIComponent(token) : "");
  }

  // 当前切片标注查询（合同 §3.1：?slide_id= 优先；旧后端 ?slide=<名>）
  function annotationsQueryFor(s) {
    var ref = s ? (s.id || s.name) : "";
    if (!ref) return null;
    if (slideIdApiOn() && s && s.id) {
      return "/api/annotations?slide_id=" + encodeURIComponent(s.id);
    }
    return "/api/annotations?slide=" + encodeURIComponent(ref);
  }

  // 行 key（data-slide-id；旧后端=name）→ 写通道载荷（合同 §3.2/§4）：
  // ID 通道发 slide_ids（解析不到 ID 的行剔除——列表外资产不参与），
  // 旧后端发 slides(名)。
  function slidePayloadRef(ref) {
    if (ref == null || ref === "") return null;
    if (!slideIdApiOn()) return ref;
    var s = findSlideInfo(ref);
    return (s && s.slide_id) || null;
  }
  function slideRefsPayload(refs) {
    var out = [];
    (refs || []).forEach(function (r) {
      var v = slidePayloadRef(r);
      if (v) out.push(v);
    });
    return slideIdApiOn() ? { slide_ids: out } : { slides: out };
  }

  // ---------- 打开切片 ----------
  // ref = slide_id（ID 通道，唯一操作键）；slideIdApiOn()=false 的旧后端由
  // 调用方传 legacy 文件名（列表行 data-slide-id 同源）。
  // 请求序号（§5.2）：A→B 快速点击只保留最后一次——晚到的旧 info 响应整体
  // 丢弃，不触碰 viewer/state/选中标记。
  var openSlideSeq = 0;

  function openSlide(ref, cardSource) {
    // 背景桥接（§4.2.1）：清理之前先冻结上一张已解码预览（翻牌层里的
    // 缩略图优先——它才是此刻用户眼前的图；否则冻结底图预览节点），
    // 换片全程画面不经过纯深色中间帧
    promotePreviousPreview();
    cancelSlideDeal();
    clearBaseThumb();
    var seq = ++openSlideSeq;
    slideOpenActive = seq;   // viewer.open 的同步 close 属于换片：保留连续图层
    if (cardSource) startSlideDeal(ref, seq, cardSource);
    apiFetch(slideInfoUrl(ref))
      .then(function (r) {
        var st = r.status;
        return r.json().then(
          function (info) { return { status: st, info: info }; },
          function () { return { status: st, info: { error: "invalid info body" } }; });
      })
      .then(function (res) {
        if (seq !== openSlideSeq) return; // 晚到的旧响应：丢弃
        // 临时查看切片被服务端拒绝（403/404=到期或无权读取，§5.5）：清屏提示。
        // 临时标记来源（DEF-3，集成验收）：info 不带 temporary_view_expires_at
        // 时回读最近一次 /api/slides 列表项——两条路任一命中即按到期处理；
        // 其余切片维持原报错路径。
        var listedInfo = findSlideInfo(ref);
        var tempMarked = (state.slide &&
            String(state.slide.id || state.slide.name) === String(ref) &&
            state.slide.temporaryViewExpiresAt) ||
          (listedInfo && listedInfo.temporary_view_expires_at);
        if ((res.status === 403 || res.status === 404) && tempMarked) {
          endTemporaryView();
          // 列表权威刷新（到期后服务端不再下发该切片）
          reloadProjectsAndUnfiled().catch(function () {});
          return;
        }
        var info = res.info;
        if (info.error) {
          // info 失败要明确失败（§4.2.7）：桥接层退场，不把旧图留在屏上
          // 冒充新图；旧切片瓦片仍在 viewer 里，由 toast 说明状态
          slideOpenActive = 0;
          cancelSlideDeal();
          clearBridgeThumb();
          toast(t("open.fail", { e: info.error }), "error");
          return;
        }
        openSlideId = String(info.slide_id || "");  // 计数高亮的切片绑定
        state.slide = {
          // slide ID 化（P2 合同 §5.1）：id = slide_id 唯一操作键；name 保持
          // legacy 文件名（显示/快照）；旧后端 DTO 无 slide_id → id 为 null，
          // 全链回落按 name 比对
          id: info.slide_id || null,
          name: info.name,
          display_name: info.display_name || info.alias || "",
          original_filename: info.original_filename || "",
          width: info.width,
          height: info.height,
          mppX: info.mpp_x,
          mppY: info.mpp_y,
          mppSource: info.mpp_source,
          // info.asset_revision（"mtime_ns:size"）随 info 响应下发；边缘路径
          // （render fields 读取失败等）可能缺键 → 宽容置 null
          revision: info.asset_revision || null,
          // 临时查看到期时间（§5.5）：真实后端把 temporary_view_expires_at
          // 放在 /api/slides 列表项上（epoch 秒），/info 不带——info 有值用
          // info，否则回读列表项；两者统一归一为毫秒时间戳（缺失=普通切片，
          // 不定时器）。原始 epoch 秒直接 new Date() 会被当毫秒 → 立即过期。
          temporaryViewExpiresAt: tempExpiryMs(
            info.temporary_view_expires_at != null
              ? info.temporary_view_expires_at
              : (listedInfo && listedInfo.temporary_view_expires_at)),
        };
        state.mppX = info.mpp_x;
        state.rotation = 0;
        // 升级 A：切片已打开，隐藏无切片空态入口
        updateViewerEmptyState();
        // §3.4：常驻 #current-slide 已下架——切片名进 document.title
        //（"切片名 · PathTogether Beta"）；侧栏选中行/切片信息菜单/面板标题仍在。
        updateDocTitle(slideDisplayName(state.slide) || info.name);
        updateMppSetterVisibility();
        exitRoi();
        // Wave 3（普通图片兼容）：缺物理标尺（mpp_source="missing"）时单位区
        // 直接落到 px 并禁用 mm/µm 预设——首次打开即可进行像素操作，不要求
        // 先填写虚假标尺；显式 setMpp 后由 syncUnitAvailability 重新放开。
        syncUnitAvailability();
        // 创建底图缩略图层：铺在瓦片 canvas 之前（下层），慢网下透出模糊预览
        // （src 由通道控制器 setThumbnail 或 legacy 路径填充）。src 优先复用
        // 飞牌手里已解码的同一张缩略图（同 URL → 缓存直接命中，§4.2.2），
        // decode 完成前桥接层保持在场。
        // Slow metadata may arrive after the 8s fallback created this layer.
        // Reuse it: overwriting the reference would orphan the visible preview.
        if (!baseThumbEl) {
          baseThumbEl = document.createElement("img");
          baseThumbEl.className = "osd-base-thumb";
          baseThumbEl.alt = "";
          viewer.container.insertBefore(baseThumbEl, viewer.canvas);
        }
        applyBaseThumbFlip();
        var dealThumbSrc = (slideDeal && slideDeal.seq === seq && slideDeal.image && slideDeal.image.src)
          ? slideDeal.image.src : null;
        // 多通道通道着色：共用组件按 info 决定 inline custom TileSource（携带
        // render token）或原 DZI 路径；RGB / flag 关返回 legacy，行为不变。
        // render 计划的 viewer.open 由控制器经 opts.open 完成（409 刷新等
        // 路径同入口）；本函数只负责 legacy 打开。
        var plan = null;
        if (window.HP_Channels) {
          channelCtrl = channelCtrl || createChannelController();
          plan = channelCtrl.handleInfo(info, {
            // 通道配色/storageKey 键：ID 通道=slide_id；旧后端回落 name
            id: info.slide_id || info.name,
            scope: userScope(),
          });
        }
        if (!plan || plan.kind !== "render") {
          baseThumbEl.src = dealThumbSrc || slideThumbnailUrl(ref);
          viewer.open(slideDziUrl(ref));
        } else if (plan.thumbnailUrl) {
          // render 计划首开（多通道 / RGB 画质路径）：底图预览由计划提供
          // （缩略图与瓦片同 context）
          baseThumbEl.src = plan.thumbnailUrl;
        } else if (dealThumbSrc) {
          baseThumbEl.src = dealThumbSrc;
        }
        armBaseThumbDecodeWatch(seq);
        // 高亮列表项（未归类与项目切片行）：按 data-slide-id（= slide_id；
        // 旧后端列表无 slide_id 时 dataset 键回落 name，P6 拆 name 回落）
        var activeRef = String(ref);
        document.querySelectorAll(".slide-row, .fb-hit").forEach(function (it) {
          var on = String(it.dataset.slideId) === activeRef;
          it.classList.toggle("active", on);
          if (it.setAttribute) it.setAttribute("aria-pressed", on ? "true" : "false");
          var dot = it.querySelector ? it.querySelector(".fb-card-dot") : null;
          if (dot) dot.textContent = on ? "●" : "";
        });
        // 临时查看：按到期时间设定清屏计时器（服务端门禁是唯一权限依据）
        armTempViewTimer(state.slide);
        updateCanvasSlideLabel();
        syncAnnoExportBtn();
        // 反馈记录器（§3）：打开切片记入 nav 事件并更新快照的当前切片
        fbRecSetSlide(info.slide_id || info.name || null);
        fbRecLog("nav", { slide_id: info.slide_id || info.name || null, phase: "slide_open" });
        // 画质/RGB/mpp 等上下文控件随切片出现 → 顶栏需重测溢出折放（P0：
        // 否则 1440 档打开切片后 ⋯/账户被推出视口）。立即 + 迟一拍各一次
        //（channel 控件挂载可能晚于本回调）。
        applyToolbarTier();
        setTimeout(function () { try { applyToolbarTier(); } catch (e) {} }, 150);
        // 手机端：打开切片后自动收起侧栏抽屉，让用户立刻看到查看器；
        // 收起后走统一布局同步（抽屉关闭 + viewer resize 链，§4.2）。
        // 桌面端保持当前收起/展开偏好，不打断读片。
        if (isMobileWidth()) sidebarCtrl.closeDrawer();
      })
      .catch(function (e) {
        if (seq === openSlideSeq) {
          slideOpenActive = 0;
          cancelSlideDeal();
          clearBridgeThumb();
          toast(t("open.info.fail", { e: e }), "error");
        }
      });
  }

  // A visual bridge only: requests start immediately and keep the existing
  // last-request-wins guard. Flight never captures input or owns viewer state.
  var slideDeal = null;

  function captureSlideCard(hit) {
    if (isMobileWidth() || (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches)) return null;
    var card = fbPulloutHit === hit && fbPulloutEl ? fbPulloutEl : hit.querySelector(".fb-card");
    var img = card && card.querySelector("img");
    // Keyboard focus can create a portal and activate it in the same frame,
    // before its cloned image has decoded. The source thumbnail is already ready.
    if (!img || !img.complete || !img.naturalWidth) img = hit.querySelector("img");
    if (!card || !card.getBoundingClientRect || !img || !img.complete || !img.naturalWidth) return null;
    var rect = card.getBoundingClientRect();
    if (!rect.width || !rect.height) rect = hit.querySelector(".fb-card").getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    var label = hit.querySelector(".fb-card-name");
    // The flight starts as an exact copy of the card the user pulled out.
    var face = (card.classList && card.classList.contains("fb-card") ? card
      : card.querySelector(".fb-card")) || null;
    return { rect: rect, src: img.currentSrc || img.src, width: img.naturalWidth, height: img.naturalHeight,
      name: label ? (label.title || label.textContent) : hit.dataset.name,
      face: face ? face.cloneNode(true) : null };
  }

  function cancelSlideDeal() {
    if (!slideDeal) return;
    var deal = slideDeal;
    slideDeal = null; // Invalidate all animation/promise callbacks first.
    clearTimeout(deal.timeout);
    deal.animations.forEach(function (animation) { animation.cancel(); });
    if (deal.el && deal.el.parentNode) deal.el.parentNode.removeChild(deal.el);
    if (deal.landing && deal.landing.parentNode) deal.landing.parentNode.removeChild(deal.landing);
  }

  function dealAnimate(deal, el, frames, options) {
    var animation = el.animate(frames, options);
    deal.animations.push(animation);
    // Cancellation is normal when a second selection supersedes this one.
    return animation.finished.catch(function () {});
  }

  function dealBoxTransform(rect, width, height) {
    return "translate3d(" + rect.left + "px," + rect.top + "px,0) scale(" +
      (rect.width / width) + "," + (rect.height / height) + ")";
  }

  function startSlideDeal(ref, seq, source) {
    var root = $("viewer");
    if (!root || !root.animate) return;
    var target = root.getBoundingClientRect();
    if (!target.width || !target.height) return;
    var el = document.createElement("div");
    el.className = "slide-deal";
    el.dataset.slideId = String(ref);
    el.dataset.phase = "drawing";
    el.setAttribute("aria-hidden", "true");
    var card = document.createElement("div");
    card.className = "slide-deal-card";
    var r = source.rect;
    var width = Math.min(320, target.width * .45), height = width * r.height / r.width;
    var center = { left: target.left + (target.width - width) / 2,
      top: target.top + (target.height - height) / 2, width: width, height: height };
    // Front: the file card itself, laid out at its sidebar size and scaled to
    // the flight box so it is pixel-identical at the start of the pull.
    // Back: the slide preview, which is what the card turns over to reveal and
    // what then spreads into the viewer.
    var front = document.createElement("div");
    front.className = "slide-deal-face slide-deal-front";
    var face = source.face;
    if (face) {
      face.className = "fb-card slide-deal-filecard";
      face.removeAttribute("id");
      face.style.width = r.width + "px";
      face.style.height = r.height + "px";
      face.style.transform = "scale(" + (width / r.width) + ")";
    } else {
      face = document.createElement("div");
      face.className = "slide-deal-label";
      face.textContent = source.name || "";
    }
    front.appendChild(face);
    var back = document.createElement("div");
    back.className = "slide-deal-face slide-deal-back";
    var img = document.createElement("img");
    img.src = source.src;
    img.alt = "";
    back.appendChild(img);
    card.appendChild(front);
    card.appendChild(back);
    el.appendChild(card);
    Object.assign(el.style, { left: "0px", top: "0px", width: width + "px", height: height + "px" });
    el.style.transform = dealBoxTransform(center, width, height);
    // 无黑幕（§4.2.1）：不再追加不透明 .slide-deal-stage 全视窗遮罩——旧切片
    // 内容/空态保持可见，背景连续性由桥接层负责，飞牌只是装饰层。
    document.body.appendChild(el);
    var deal = slideDeal = { el: el, card: card, front: front, image: img, seq: seq,
      ref: String(ref), center: center, imageWidth: source.width, imageHeight: source.height,
      thumbSrc: img.src, animations: [], drawn: false, spreading: false,
      landed: false, finishing: false, tilesCovered: false, rect: null, opened: false };
    // A failed/very slow DZI must never leave a decorative card over the workbench.
    // 8 秒仍加载中（§4.2.7）：先把完整预览移交底图再退出装饰，不重新露出黑屏。
    deal.timeout = setTimeout(function () {
      if (slideDeal !== deal) return;
      if (deal.landed && deal.landing) {
        deal.finishing = true;
        handoffPreviewToBase(deal);
        toast(t("open.slow"), "info");
        dealAnimate(deal, deal.el, [{ opacity: 1 }, { opacity: 0 }], { duration: 160, fill: "forwards" });
        whenThumbDecoded(baseThumbEl).then(function () {
          if (slideDeal !== deal) return;
          dealAnimate(deal, deal.landing, [{ opacity: 1 }, { opacity: 0 }],
            { duration: 160, fill: "forwards" }).then(function () {
              if (slideDeal === deal) cancelSlideDeal();
            });
        });
        return;
      }
      cancelSlideDeal();
    }, 8000);
    // 1) Pull: slide straight out of the stack at the card's own size until it
    //    clears the sidebar edge. 2) Turn: move to the viewer centre while the
    //    card turns over around its vertical axis to show the slide.
    //    The spread (spreadSlideDeal) then grows that face into the viewer.
    // Geometry is fixed once. Motion stays in transform instead of forcing
    // layout and repaint on every frame of the flight.
    var pulledLeft = Math.max(r.left + r.width * .6, target.left + 18);
    pulledLeft = Math.min(pulledLeft, window.innerWidth - r.width - 8);
    var pulled = { left: Math.max(r.left, pulledLeft), top: r.top, width: r.width, height: r.height };
    var DEAL_MS = 620, PULL_END = .30;
    dealAnimate(deal, el, [
      { transform: dealBoxTransform(r, width, height), offset: 0, easing: "cubic-bezier(.5,0,.3,1)" },
      { transform: dealBoxTransform(pulled, width, height), offset: PULL_END, easing: "cubic-bezier(.35,.1,.2,1)" },
      { transform: dealBoxTransform(center, width, height) }],
      { duration: DEAL_MS, easing: "linear", fill: "both" });
    dealAnimate(deal, card, [
      { transform: "rotateY(0deg) rotateZ(0deg)", offset: 0 },
      // Start turning as the card clears the stack; finish at the instant
      // spreading starts, without a motionless face-up beat in the centre.
      { transform: "rotateY(0deg) rotateZ(-1.5deg)", offset: PULL_END, easing: "cubic-bezier(.45,0,.25,1)" },
      { transform: "rotateY(180deg) rotateZ(0deg)" }
    ], { duration: DEAL_MS, easing: "linear", fill: "both" }).then(function () {
      if (slideDeal !== deal) return;
      deal.drawn = true;
      spreadSlideDeal(deal);
    });
  }

  function spreadSlideDeal(deal) {
    if (slideDeal !== deal || !deal.drawn || deal.spreading) return;
    // Requests run alongside the flight. Never wait for metadata or tiles to
    // expand the already decoded thumbnail; use OSD bounds when available and
    // otherwise fit the thumbnail's own aspect ratio into the same viewport.
    var root = $("viewer").getBoundingClientRect();
    var rect;
    if (deal.opened) try {
      var tl = viewer.viewport.imageToViewerElementCoordinates(new OpenSeadragon.Point(0, 0));
      var br = viewer.viewport.imageToViewerElementCoordinates(new OpenSeadragon.Point(state.slide.width, state.slide.height));
      rect = { left: root.left + Math.min(tl.x, br.x), top: root.top + Math.min(tl.y, br.y),
        width: Math.abs(br.x - tl.x), height: Math.abs(br.y - tl.y) };
    } catch (e) { /* Metadata may still be in flight. */ }
    if (!rect && deal.imageWidth > 0 && deal.imageHeight > 0) {
      var fit = Math.min(root.width / deal.imageWidth, root.height / deal.imageHeight);
      var w = deal.imageWidth * fit, h = deal.imageHeight * fit;
      rect = { left: root.left + (root.width - w) / 2, top: root.top + (root.height - h) / 2,
        width: w, height: h };
    }
    if (!rect || !rect.width || !rect.height) { cancelSlideDeal(); return; }
    // Resizing/zooming during flight should hand control straight back to OSD.
    if (rect.width > root.width + 2 || rect.height > root.height + 2) { cancelSlideDeal(); return; }
    deal.spreading = true;
    deal.el.dataset.phase = "spreading";
    deal.rect = rect;   // 目标几何（§4.2.5 可交接定义的输入之一）
    var c = deal.center;
    // Rasterize the landing image at its final size once, then scale it
    // uniformly. Stretching the whole card would distort panoramic/portrait
    // slides; resizing its width/height would repaint the image every frame.
    var landing = document.createElement("div");
    landing.className = "slide-deal-landing";
    landing.setAttribute("aria-hidden", "true");
    landing.style.width = rect.width + "px";
    landing.style.height = rect.height + "px";
    landing.style.transform = dealBoxTransform(rect, rect.width, rect.height);
    // Reuse the decoded thumbnail: moving it adds no image request or decode.
    landing.appendChild(deal.image);
    document.body.appendChild(landing);
    deal.landing = landing;
    var scale = Math.min(c.width / rect.width, c.height / rect.height);
    var inset = { left: c.left + (c.width - rect.width * scale) / 2,
      top: c.top + (c.height - rect.height * scale) / 2,
      width: rect.width * scale, height: rect.height * scale };
    dealAnimate(deal, deal.el, [{ opacity: 1 }, { opacity: 0 }], { duration: 160, fill: "forwards" });
    dealAnimate(deal, landing, [
      { transform: dealBoxTransform(inset, rect.width, rect.height) },
      { transform: dealBoxTransform(rect, rect.width, rect.height) }
    ], { duration: 340, easing: "cubic-bezier(.2,.7,.2,1)", fill: "forwards" }).then(function () {
      if (slideDeal !== deal) return;
      deal.landed = true;
      deal.el.dataset.phase = "preview";
      // 铺开完成即具备桥接退场的一半条件（viewer 已 open）——若新预览也已
      // decode，就让桥接层退场，避免双份旧图长驻
      maybeRetireBridgeThumb();
      finishSlideDeal(deal);
    });
  }

  // 「可交接」（§4.2.5）：selection generation 匹配（seq/opened 由前置守卫保
  // 证）+ 目标几何有效 + 完整底图已解码可见，或目标可见图像区域已被已绘制
  // 且完成必要混合的瓦片覆盖（tilesCovered 只能由 tiled-image-drawn 的逐帧
  // 覆盖判定置位，不能只凭 e.tiles.length>0）。
  function dealHandoffReady(deal) {
    if (slideDeal !== deal || !deal.landed || deal.finishing) return false;
    if (!deal.rect || !(deal.rect.width > 0) || !(deal.rect.height > 0)) return false;
    if (deal.seq !== openSlideSeq || !deal.opened) return false;
    if (thumbDecoded(baseThumbEl)) return true;
    return !!deal.tilesCovered;
  }

  function finishSlideDeal(deal) {
    if (!dealHandoffReady(deal)) return;
    deal.finishing = true;
    deal.el.dataset.phase = "handoff";
    // 交接顺序（§4.2.4）：先把完整预览交给 viewer 底图层（OSD 透明区域下方
    // 已是同一张已解码预览，对齐几何），再淡出飞牌副本——不存在两处都空的帧。
    syncBaseThumb();
    var landingFade = dealAnimate(deal, deal.landing, [{ opacity: 1 }, { opacity: 0 }], { duration: 180, fill: "forwards" });
    // 桥接层（若还在）与飞牌副本同时退场：新预览已在它们下方完成交接
    retireBridgeThumb();
    var cardFade = dealAnimate(deal, deal.el, [{ opacity: 1 }, { opacity: 0 }], { duration: 160, fill: "forwards" });
    Promise.all([landingFade, cardFade]).then(function () {
      if (slideDeal === deal) cancelSlideDeal();
    });
  }

  // 8 秒超时的预览移交（§4.2.7）：把飞牌里已解码的完整预览写回底图层并
  // 对位；底图 decode 兑现前飞牌保持可见，不一次移除所有可见预览。
  function handoffPreviewToBase(deal) {
    if (!viewer || !viewer.container) return;
    if (!baseThumbEl) {
      baseThumbEl = document.createElement("img");
      baseThumbEl.className = "osd-base-thumb";
      baseThumbEl.alt = "";
      viewer.container.insertBefore(baseThumbEl, viewer.canvas);
      applyBaseThumbFlip();
    }
    if (!thumbDecoded(baseThumbEl) && thumbDecoded(deal.image) && deal.image.src) {
      baseThumbEl.src = deal.image.src;   // 同 URL → 缓存直接命中
    }
    if (deal.rect) {
      var containerRect = viewer.container.getBoundingClientRect();
      baseThumbEl.style.left = (deal.rect.left - containerRect.left) + "px";
      baseThumbEl.style.top = (deal.rect.top - containerRect.top) + "px";
      baseThumbEl.style.width = deal.rect.width + "px";
      baseThumbEl.style.height = deal.rect.height + "px";
      baseThumbEl.style.display = "block";
    }
  }

  // 可见图像区域的瓦片覆盖判定（§4.2.5）：e.tiles 是本帧实际绘制完成并
  // 混合的瓦片（图像坐标 bounds），对当前 viewport 在该图层上的可见矩形
  // 做网格采样近似并集覆盖率。
  function tilesCoverVisibleViewport(tiledImage, tiles) {
    try {
      if (!tiledImage || !tiles || !tiles.length || !viewer || !viewer.viewport) return false;
      var vis = tiledImage.viewportToImageRectangle(viewer.viewport.getBounds(true));
      var dim = tiledImage.getContentSize ? tiledImage.getContentSize() : tiledImage.source.dimensions;
      var x0 = Math.max(0, vis.x), y0 = Math.max(0, vis.y);
      var x1 = Math.min(dim.x, vis.x + vis.width), y1 = Math.min(dim.y, vis.y + vis.height);
      var vw = x1 - x0, vh = y1 - y0;
      if (!(vw > 0) || !(vh > 0)) return false;
      var N = 20, hit = 0;
      for (var i = 0; i < N; i++) {
        for (var j = 0; j < N; j++) {
          var px = x0 + vw * (i + .5) / N, py = y0 + vh * (j + .5) / N;
          for (var k = 0; k < tiles.length; k++) {
            var b = tiles[k] && tiles[k].bounds;
            if (b && px >= b.x && px <= b.x + b.width && py >= b.y && py <= b.y + b.height) {
              hit++;
              break;
            }
          }
        }
      }
      return hit / (N * N) >= 0.98;
    } catch (e) { return false; }
  }

  // ---------- 临时查看到期（§5.5） ----------
  // 到期（计时器）或读取被拒（403/404）→ 关闭画面、移除该切片卡片与缩略图、
  // 提示「临时查看已结束，可在后台重新开启」。服务端门禁是唯一权限依据，
  // 计时器只负责清屏。
  var tempViewTimer = null;

  // 到期时间归一为毫秒时间戳：真实后端列表下发 epoch 秒；/info 可能带同样
  // 的秒值；既有夹具/旧口径也接受 ISO 字符串。无效/缺失 → null（不定时器）。
  // 阈值 1e11：2026 年的 epoch 秒 ≈1.8e9、毫秒 ≈1.8e12，秒值绝无可能超过
  // 1e11（那是公元 5138 年），据此区分两种单位。
  function tempExpiryMs(v) {
    if (v == null || v === "") return null;
    if (typeof v === "number") {
      if (!isFinite(v)) return null;
      return v > 1e11 ? Math.round(v) : v * 1000;
    }
    var raw = String(v).trim();
    if (!raw) return null;
    if (/^-?\d+(\.\d+)?$/.test(raw)) {
      var n = Number(raw);
      return n > 1e11 ? Math.round(n) : n * 1000;
    }
    var parsed = Date.parse(raw);
    return isFinite(parsed) ? parsed : null;
  }

  function clearTempViewTimer() {
    if (tempViewTimer) {
      clearTimeout(tempViewTimer);
      tempViewTimer = null;
    }
  }

  function armTempViewTimer(slide) {
    clearTempViewTimer();
    if (!slide) return;
    var expiryMs = tempExpiryMs(slide.temporaryViewExpiresAt);
    if (expiryMs == null) return;
    var ms = expiryMs - Date.now();
    if (!isFinite(ms)) return;
    if (ms <= 0) { endTemporaryView(); return; }
    tempViewTimer = setTimeout(function () { endTemporaryView(); }, ms);
  }

  // 深链竞态补设（?slide=<id> 直接打开）：info 响应可能先于 /api/slides 到达，
  // openSlide 当时读不到列表上的临时标记（真实后端标记只在列表下发）。列表
  // 每次加载/刷新后为当前打开的切片补读一次标记并补设计时器（已有标记不动）。
  function rearmTempViewTimerFromList() {
    if (!state.slide || state.slide.temporaryViewExpiresAt) return;
    var listed = findSlideInfo(state.slide.id || state.slide.name);
    if (!listed || !listed.temporary_view_expires_at) return;
    state.slide.temporaryViewExpiresAt = tempExpiryMs(listed.temporary_view_expires_at);
    armTempViewTimer(state.slide);
  }

  // ---------- 无切片基线状态（初始 / 删除 / 临时到期共用） ----------
  // 关闭画面并把 UI 回落到「从未打开切片」的初始口径：空态卡回归、画布标签
  // 清空、缩放徽章复位「—」、画质档与通道 chrome（通道钮/RGB 徽章/面板）
  // 隐藏——任何切片上下文控件都不得残留。
  function enterNoSlideUiState() {
    cancelSlideDeal();
    var closedRef = state.slide ? String(state.slide.id || state.slide.name || "") : "";
    state.slide = null;
    openSlideId = "";
    countHighlight = null;
    state.mppX = null;
    state.roiMode = null;
    syncAnnoExportBtn();
    clearTempViewTimer();
    // 复位换片标记：随后的 viewer.close 是显式清屏，close 处理器会连桥接
    // 层一起清理（临时查看到期必须同时清掉保留的背景/预览，§4.2.8）
    slideOpenActive = 0;
    updateDocTitle(null);
    updateMppSetterVisibility();
    // 拖框/画框手势未落盘就换片：会话一并收尾，旧事件不得落到新切片
    if (roiBox || dragInfo || rectDrawInfo) exitRoi();
    if (viewer) viewer.close();
    clearBaseThumb();
    clearBridgeThumb();
    updateCanvasSlideLabel();
    updateViewerEmptyState();
    // 反馈记录器（§3）：清屏（临时查看到期等）记入 nav 事件
    if (closedRef) {
      fbRecSetSlide(null);
      fbRecLog("nav", { slide_id: closedRef, phase: "slide_close" });
    }
    // 缩放徽章直接复位到初始模板口径（不依赖 OSD close 后的内部状态）
    if (els.zoomBadge) els.zoomBadge.textContent = "—";
    if (els.headerZoomBadge) els.headerZoomBadge.textContent = "—";
    // 通道钮/RGB 徽章/通道面板回初始隐藏（destroy 同时作废在途响应；
    // 下一次 handleInfo 开头会再次 destroy，复用安全）
    if (channelCtrl && channelCtrl.destroy) channelCtrl.destroy();
    // 画质档（标准/精细）隐藏并清当前 display 状态
    if (window.HP_ViewerEncoding && HP_ViewerEncoding.resetForClose) {
      HP_ViewerEncoding.resetForClose();
    }
    // 上下文控件消失/出现 → 顶栏重测溢出折放
    setTimeout(function () { try { applyToolbarTier(); } catch (e) {} }, 150);
  }

  function endTemporaryView() {
    clearTempViewTimer();
    var cur = state.slide;
    enterNoSlideUiState();
    // 本地标记已结束：卡片与缩略图立即移除，再从 /api/slides 拉权威状态
    //（到期后服务端不再下发该切片；DEF-3 验收要求「刷新列表」）
    if (cur && (cur.id || cur.name)) {
      var s = findSlideInfo(cur.id || cur.name);
      if (s) s.__tempEnded = true;
    }
    renderFolderBrowser();
    toast(t("tempview.ended"), "info");
    reloadProjectsAndUnfiled().catch(function () {});
  }

  // ---------- 当前切片名画布标签（§5.1：紧凑标签，不新增第二行） ----------
  function updateCanvasSlideLabel() {
    var el = els.canvasSlideLabel;
    if (!el) return;
    var name = state.slide
      ? (slideDisplayName(state.slide) || state.slide.name || "")
      : "";
    if (el.textContent !== undefined) el.textContent = name;
    el.hidden = !name;
  }

  // ---------- mpp 设置区显示控制 ----------
  function updateMppSetterVisibility() {
    if (!state.slide) { els.mppSetter.style.display = "none"; return; }
    var src = state.slide.mppSource;
    if (src === "missing" || src === "estimated") {
      els.mppSetter.style.display = "flex";
      els.mppInput.value = state.mppX != null ? state.mppX : "";
    } else {
      els.mppSetter.style.display = "none";
    }
  }

  // ---------- 无物理标尺（普通图片 mpp_source="missing"）单位区可用性 ----------
  // Wave 3 产品语义（§4.4）：没有可信 MPP 时保留像素尺寸/像素标注/缩放百分比，
  // 物理单位（mm/µm）操作禁用并明确提示，而不是要求先填一个虚假标尺。
  // 显式手动校准（setMpp，前端态）后 mppX/mppY 有效，物理单位重新可用。
  function slideHasPhysicalScale() {
    return posNum(Number(state.mppX)) && posNum(Number(state.slide && state.slide.mppY));
  }
  function syncUnitAvailability() {
    var hasPhys = !!state.slide && slideHasPhysicalScale();
    if (els.roiUnitSelect) {
      var opts = els.roiUnitSelect.options || [];
      Array.prototype.forEach.call(opts, function (opt) {
        if (opt && opt.value !== "px") opt.disabled = !hasPhys;
      });
      if (!hasPhys && state.roiUnit !== "px") {
        // 普通图片首次打开：单位默认直接落到像素，不停在 mm 上报错
        state.roiUnit = "px";
        els.roiUnitSelect.value = "px";
      }
    }
    if (els.roiPresetSelect) {
      // mm 预设（6/6.5）属于物理单位操作，一并禁用
      els.roiPresetSelect.disabled = !hasPhys;
    }
    if (els.roiNoScaleHint) {
      els.roiNoScaleHint.hidden = hasPhys || !state.slide;
      if (!els.roiNoScaleHint.hidden) {
        els.roiNoScaleHint.textContent = tt("roi.no.scale.hint");
      }
    }
  }

  // ---------- 缩放 / 旋转 / 复位 ----------
  // P3 研究采集：缩放按钮/快捷键是人工输入（§7.1），进入采集模块的
  // gesture context；采集模块由 initResearchTelemetry 在采集开关开启时装配，
  // 未装配时这里是无操作。
  function notifyResearchZoomTool() {
    if (researchTelemetry) researchTelemetry.notifyToolInteraction({ inputKind: "button" });
  }
  function zoomIn() {
    if (!viewer || !viewer.viewport) return;
    notifyResearchZoomTool();
    viewer.viewport.zoomBy(1.4);
    viewer.viewport.applyConstraints();
  }
  function zoomOut() {
    if (!viewer || !viewer.viewport) return;
    notifyResearchZoomTool();
    viewer.viewport.zoomBy(1 / 1.4);
    viewer.viewport.applyConstraints();
  }
  // 1:1 原始像素（F3）：口径唯一在 HP_ViewerCore.zoomToNative；其未加载时才本地兜底
  function zoomNative() {
    if (window.HP_ViewerCore && HP_ViewerCore.zoomToNative) {
      HP_ViewerCore.zoomToNative(viewer);
      return;
    }
    try {
      if (!viewer || !viewer.viewport || !viewer.source) return;
      var vp = viewer.viewport;
      vp.zoomTo(viewer.source.dimensions.x / vp.getContainerSize().x, vp.getCenter());
      vp.applyConstraints();
    } catch (e) { /* 忽略 */ }
  }
  function rotate() {
    if (!viewer || !viewer.viewport) return;
    state.rotation = (state.rotation + 90) % 360;
    viewer.viewport.setRotation(state.rotation);
    updateRoiOverlay();
    redrawAnnoCanvas();
  }
  function flip() {
    if (!viewer || !viewer.viewport || !viewer.viewport.toggleFlip) return;
    viewer.viewport.toggleFlip();
    // 'flip' 事件负责同步 state/各层；toggleFlip 可能未触发事件时兜底
    state.flipped = !!viewer.viewport.getFlip();
    applyBaseThumbFlip();
    syncBaseThumb();
    redrawAnnoCanvas();
    updateRoiOverlay();
  }
  function reset() {
    if (!viewer || !viewer.viewport) return;
    state.rotation = 0;
    viewer.viewport.setRotation(0);
    // 复位时取消镜像（回到默认朝向）
    if (viewer.viewport.getFlip && viewer.viewport.getFlip()) {
      viewer.viewport.toggleFlip();
    }
    viewer.viewport.goHome(true);
  }

  // ---------- 矩形工具（升级 C §6.1/§6.2） ----------
  // 权威几何：level-0 像素 x/y/w/h（state.roi）。物理单位仅是输入/展示口径：
  //   w_px = round(width × 1000 / mpp_x)、h_px = round(height × 1000 / mpp_y)
  //   （μm 不乘 1000）；显示值按实际像素 + 分轴校准反算。
  // 创建只受单边上限 40000px + 切片边界约束（出界保持上个合法框）；
  // w*h 面积预算是导出/裁剪闸（saveCrop 交服务端 crop_guard，超限 413 明确
  // 报错），与「矩形跨度」不是同一件事，创建不设面积闸（大 ROI 是旧主流工作流）。

  // rect 单边像素上限（与后端 RECT_MAX_SIDE_PX 一致）
  var RECT_MAX_SIDE_PX = 40000;

  function rectToolActive() { return state.roiMode === "rect"; }
  function roiW() { return state.roi ? state.roi.w : 0; }
  function roiH() { return state.roi ? state.roi.h : 0; }

  // 数值是否可用（有限且 > 0）
  function posNum(v) { return typeof v === "number" && isFinite(v) && v > 0; }

  // 当前单位 → 像素的换算系数（分轴 MPP，§6.2）。返回 [kx, ky]（px per unit）
  // 或 null（物理单位缺任一轴可信 MPP：像素可用、物理单位需先校准）。
  function unitToPxFactors(unit) {
    if (unit === "px") return [1, 1];
    var mx = Number(state.mppX);
    var my = Number(state.slide && state.slide.mppY);
    if (unit === "mm") { mx = mx / 1000; my = my / 1000; } // µm/px → mm/px
    if (!posNum(mx) || !posNum(my)) return null;
    return [1 / mx, 1 / my]; // px per (mm|µm)
  }

  // 把设置区数值（当前单位）换算为像素 w/h；非法/缺 MPP 返回 null（附错误文案 key）
  function rectInputsToPx() {
    var unit = state.roiUnit;
    var wv = parseFloat(els.roiWInput && els.roiWInput.value);
    var hv = parseFloat(els.roiHInput && els.roiHInput.value);
    if (!isFinite(wv) || !isFinite(hv) || wv <= 0 || hv <= 0) return null;
    if (unit === "px") return { w: Math.round(wv), h: Math.round(hv) };
    var f = unitToPxFactors(unit);
    if (!f) return null;
    return { w: Math.round(wv * f[0]), h: Math.round(hv * f[1]) };
  }

  // 像素 → 物理展示串（分轴反算；mm 保留 2 位、μm 保留 1 位、px 原样）
  function rectPhysicalText(w, h) {
    var unit = state.roiUnit;
    if (unit === "px") return w + " × " + h + " px";
    var mx = Number(state.mppX), my = Number(state.slide && state.slide.mppY);
    if (!posNum(mx) || !posNum(my)) return w + " × " + h + " px";
    var wx = w * mx, hy = h * my; // µm
    if (unit === "mm") {
      return (wx / 1000).toFixed(2) + " × " + (hy / 1000).toFixed(2) + " mm";
    }
    return wx.toFixed(1) + " × " + hy.toFixed(1) + " μm";
  }

  // 矩形几何归一（§6.2）：负方向归一、整数、边界约束
  // x>=0,y>=0,w>=1,h>=1,x+w<=W,y+h<=H。非法输入（NaN/Infinity/负值）返回 null。
  // 不做 w*h 面积闸：创建/编辑必须保留用户真实几何，不得静默缩放或截停；
  // 超预算只在导出/裁剪时由服务端 crop_guard 明确报错。
  function normalizeRect(x0, y0, x1, y1) {
    var W = state.slide.width, H = state.slide.height;
    if (![x0, y0, x1, y1].every(isFinite)) return null;
    var x = Math.max(0, Math.round(Math.min(x0, x1)));
    var y = Math.max(0, Math.round(Math.min(y0, y1)));
    var w = Math.round(Math.abs(x1 - x0));
    var h = Math.round(Math.abs(y1 - y0));
    w = Math.min(Math.max(w, 1), RECT_MAX_SIDE_PX);
    h = Math.min(Math.max(h, 1), RECT_MAX_SIDE_PX);
    x = Math.min(x, Math.max(0, W - w));
    y = Math.min(y, Math.max(0, H - h));
    if (x < 0 || y < 0 || w < 1 || h < 1 || x + w > W || y + h > H) return null;
    return { x: x, y: y, w: w, h: h };
  }

  // 已有矩形的等比换算（锁定比例时拖边/输数用）：按高度推宽度
  function lockRatioAdjust(wPx, hPx) {
    if (!state.roiLockRatio || !(state.roi.w > 0) || !(state.roi.h > 0)) {
      return { w: wPx, h: hPx };
    }
    var ratio = state.roi.w / state.roi.h;
    // 以较大变化轴为准推另一轴
    if (Math.abs(hPx - state.roi.h) > Math.abs(wPx - state.roi.w)) {
      return { w: Math.max(1, Math.round(hPx * ratio)), h: hPx };
    }
    return { w: wPx, h: Math.max(1, Math.round(wPx / ratio)) };
  }

  // 在给定中心放置 w×h（clamp 到切片内，保大小移到边缘）
  function placeRectAtCenter(cx, cy, w, h) {
    var W = state.slide.width, H = state.slide.height;
    var x = clamp(Math.round(cx - w / 2), 0, Math.max(0, W - w));
    var y = clamp(Math.round(cy - h / 2), 0, Math.max(0, H - h));
    state.roi.x = x; state.roi.y = y; state.roi.w = w; state.roi.h = h;
  }

  // 进入/退出矩形工具（与箭头/自由手绘互斥；完成/取消后恢复导航）
  function toggleRectTool() {
    if (rectToolActive()) { exitRoi(); return; }
    if (!state.slide) { toast(t("roi.need.slide"), "error"); return; }
    var mx = Number(state.mppX), my = Number(state.slide.mppY);
    if (state.roiUnit !== "px" && (!posNum(mx) || !posNum(my))) {
      toast(t("roi.need.mpp"), "error");
      return;
    }
    if (state.slide.mppSource === "estimated") {
      toast(t("roi.estimate.tip"), "info");
    }
    // 与 arrow/freehand 互斥
    exitDrawMode();
    state.roiMode = "rect";
    state.roi = { x: 0, y: 0, w: 0, h: 0 };
    els.annoCanvas.classList.add("drawing");
    els.roiRectBtn.classList.add("active");
    if (els.roiSettings) {
      els.roiSettings.hidden = false;
      // §3.3：尺寸区改为按钮下方锚定 popover（fixed 定位，JS 计算）
      positionToolbarPop(els.roiRectBtn, els.roiSettings);
    }
    els.roiRectBtn.setAttribute("aria-expanded", "true");
    if (viewer) viewer.setMouseNavEnabled(false);
    updateRoiButtons();
    updateCtxBar();
    toast(t("roi.rect.tip"), "info");
    updateResearchBusy();  // P3：矩形工具激活取消稳定观察检测
  }

  function exitRoi() {
    cancelRectPointerSessions(); // 退出工具 = 活动手势一并收尾（幂等；§3.2）
    state.roiMode = null;
    state.roiGeometryLocked = false;  // §6.3：草稿锁只对当前草稿有效
    closeAnnoToolbar();
    // 工单 D：退出工具 = 放弃未保存矩形草稿（含失败重试态）
    if (retryDraft && retryDraft.kind === "rect") retryDraft = null;
    setDrawUnsaved(false);
    setDrawPhase("idle");
    updateResearchBusy();  // P3
    if (roiBox && viewer && viewer.currentOverlays) {
      try { viewer.removeOverlay(roiBox); } catch (e) {}
    }
    if (roiBox && roiBox.parentNode) roiBox.parentNode.removeChild(roiBox);
    roiBox = null;
    if (els.annoCanvas) els.annoCanvas.classList.remove("drawing");
    if (els.roiRectBtn) {
      els.roiRectBtn.classList.remove("active");
      els.roiRectBtn.setAttribute("aria-expanded", "false");
    }
    if (els.roiSettings) els.roiSettings.hidden = true;
    if (els.roiSummary) els.roiSummary.hidden = true;
    if (viewer) viewer.setMouseNavEnabled(true);
    updateRoiButtons();
    els.saveBtn.disabled = true;
    els.saveAnnoBtn.disabled = true;
    updateCtxBar();
  }

  function updateRoiButtons() {
    if (els.roiRectBtn) {
      els.roiRectBtn.classList.toggle("active", rectToolActive());
    }
    syncRoiSettings();
  }

  // 紧凑设置区：数值随选区/单位同步（显示按实际像素反算，§6.2）
  function syncRoiSettings() {
    if (!els.roiWInput || !els.roiHInput) return;
    if (!(state.roi.w > 0)) return; // 无选区时保留用户输入
    var unit = state.roiUnit;
    if (unit === "px") {
      els.roiWInput.value = state.roi.w;
      els.roiHInput.value = state.roi.h;
      return;
    }
    var mx = Number(state.mppX), my = Number(state.slide && state.slide.mppY);
    if (!posNum(mx) || !posNum(my)) return;
    if (unit === "mm") {
      els.roiWInput.value = +(state.roi.w * mx / 1000).toFixed(2);
      els.roiHInput.value = +(state.roi.h * my / 1000).toFixed(2);
    } else {
      els.roiWInput.value = +(state.roi.w * mx).toFixed(1);
      els.roiHInput.value = +(state.roi.h * my).toFixed(1);
    }
  }

  // 设置区数值确认（Enter/change）：以当前中心调整；出界保持上个合法框并提示
  function applyRectInputs() {
    if (!state.slide || !rectToolActive()) return;
    var px = rectInputsToPx();
    if (!px) {
      var unit = state.roiUnit;
      if (unit !== "px" && !unitToPxFactors(unit)) { toast(t("roi.need.mpp"), "error"); }
      else { toast(t("roi.input.invalid"), "error"); }
      syncRoiSettings();
      return;
    }
    px = lockRatioAdjust(px.w, px.h);
    var W = state.slide.width, H = state.slide.height;
    if (px.w > W || px.h > H) { toast(t("roi.input.invalid"), "error"); syncRoiSettings(); return; }
    var cx = state.roi.x + state.roi.w / 2;
    var cy = state.roi.y + state.roi.h / 2;
    if (!(state.roi.w > 0)) { cx = W / 2; cy = H / 2; }
    placeRectAtCenter(cx, cy, px.w, px.h);
    createRoiBox();
    updateRoiOverlay();
    els.saveBtn.disabled = false;
    els.saveAnnoBtn.disabled = false;
  }

  function createRoiBox() {
    if (roiBox) return;
    roiBox = document.createElement("div");
    roiBox.id = "roi-box";
    // 四角（双轴）+ 四边（单轴）手柄（§6.1：边改单轴、角改双轴）
    ["tl", "tr", "bl", "br", "t", "b", "l", "r"].forEach(function (id) {
      var hd = document.createElement("div");
      hd.className = "roi-handle roi-" + id;
      hd.dataset.handle = id;
      roiBox.appendChild(hd);
    });
    var label = document.createElement("div");
    label.className = "roi-label";
    roiBox.appendChild(label);
    roiBox.addEventListener("pointerdown", onRoiPointerDown);
    viewer.container.appendChild(roiBox);
  }

  function updateRoiOverlay() {
    if (!roiBox || !state.slide) return;
    var r = state.roi;
    if (!(r.w > 0) || !(r.h > 0)) return;
    var label = roiBox.querySelector(".roi-label");
    if (label) label.textContent = rectPhysicalText(r.w, r.h);
    var rect = viewer.viewport.imageToViewportRectangle(r.x, r.y, r.w, r.h);
    var existing = viewer.getOverlayById(roiBox);
    if (existing) {
      viewer.updateOverlay(roiBox, rect, OpenSeadragon.Placement.TOP_LEFT);
    } else {
      var opts = { element: roiBox, location: rect,
                   placement: OpenSeadragon.Placement.TOP_LEFT };
      if (state.rotation % 360 !== 0 && OpenSeadragon.OverlayRotationMode &&
          OpenSeadragon.OverlayRotationMode.BOUNDING_BOX) {
        opts.rotationMode = OpenSeadragon.OverlayRotationMode.BOUNDING_BOX;
      }
      viewer.addOverlay(opts);
    }
    syncRoiSettings();
    updateRoiSummary();
    // §6.1：临时框创建后出现就近工具条（同目标不重建，拖动帧只重定位）
    if (rectToolActive() && state.roi.w > 0) openAnnoToolbar();
  }

  // 主行简短摘要（§3.3）：有选区时显示如「6×6 mm」，无选区隐藏。
  // 与 rectPhysicalText 同口径（分轴反算、当前单位），去掉尾随 0。
  function rectSummaryText() {
    var r = state.roi;
    if (!r || !(r.w > 0) || !(r.h > 0)) return "";
    var unit = state.roiUnit;
    if (unit === "px") return r.w + "×" + r.h + " px";
    var mx = Number(state.mppX), my = Number(state.slide && state.slide.mppY);
    if (!posNum(mx) || !posNum(my)) return r.w + "×" + r.h + " px";
    var wx = r.w * mx, hy = r.h * my; // µm
    function trimNum(n) {
      return String(n).replace(/\.0$/, "").replace(/(\.\d*?)0+$/, "$1");
    }
    if (unit === "mm") {
      return trimNum(+(wx / 1000).toFixed(2)) + "×" + trimNum(+(hy / 1000).toFixed(2)) + " mm";
    }
    return trimNum(+wx.toFixed(1)) + "×" + trimNum(+hy.toFixed(1)) + " μm";
  }

  function updateRoiSummary() {
    if (!els.roiSummary) return;
    var s = rectSummaryText();
    els.roiSummary.textContent = s;
    els.roiSummary.hidden = !s;
  }

  // ---------- 矩形拖拽：内部平移 / 四边单轴 / 四角双轴（pointer 捕获） ----------
  // 一次手势只归属一个活动 pointerId（§3.2）：
  //   - move/up/cancel 先校验 pointerId，其他指针的事件一律忽略；
  //   - lostpointercapture = 手势失效 → 取消恢复拖前几何，绝不按完成提交；
  //   - move 发现 buttons===0（真实释放已在别处丢失）→ 终止陈旧手势，
  //     既不让悬停改写几何，也不把这条兜底当一次有效自动提交；
  //   - 先清会话状态再 releasePointerCapture：正常释放随后触发的
  //     lostpointercapture 回调因此找不到任何活动会话，不会错误回滚。

  // 手势监听装配：捕获成功时补 lostpointercapture；捕获申请失败（或未生效）
  // 时改挂 document 捕获阶段兜底监听，结束时移除，绝不留下只能依靠元素自身
  // 收到 pointerup 才能收尾的拖拽。
  // elBound=true：元素级 move/up/cancel 已由常驻路由（anno-canvas 的
  // onAnnoPointer* → 矩形处理器）承担，这里不得重复注册/拆除常驻监听。
  function bindRectPointerSession(el, info, onMove, onEnd, onCancel, elBound) {
    var captured = false;
    try {
      el.setPointerCapture(info.pointerId);
      captured = !!(el.hasPointerCapture && el.hasPointerCapture(info.pointerId));
    } catch (err) { captured = false; }
    if (captured) {
      info.onLost = function (e) {
        if (e.pointerId === info.pointerId) onCancel(e);
      };
      el.addEventListener("lostpointercapture", info.onLost);
    } else {
      var doc = el.ownerDocument || document;
      info.fallback = {
        move: function (e) { if (e.pointerId === info.pointerId) onMove(e); },
        end: function (e) {
          if (e.pointerId !== info.pointerId) return;
          if (e.type === "pointercancel") onCancel(e); else onEnd(e);
        },
      };
      doc.addEventListener("pointermove", info.fallback.move, true);
      doc.addEventListener("pointerup", info.fallback.end, true);
      doc.addEventListener("pointercancel", info.fallback.end, true);
    }
    if (!elBound) {
      el.addEventListener("pointermove", onMove);
      el.addEventListener("pointerup", onEnd);
      el.addEventListener("pointercancel", onCancel);
    }
  }

  // 手势监听拆除（幂等）：调用方必须已把会话状态置空——这里最后才释放捕获，
  // 保证释放动作本身触发的 lostpointercapture 无会话可取消。
  function unbindRectPointerSession(el, info, onMove, onEnd, onCancel, elBound) {
    if (!el) return;
    if (info.onLost) {
      el.removeEventListener("lostpointercapture", info.onLost);
      info.onLost = null;
    }
    if (info.fallback) {
      var doc = el.ownerDocument || document;
      doc.removeEventListener("pointermove", info.fallback.move, true);
      doc.removeEventListener("pointerup", info.fallback.end, true);
      doc.removeEventListener("pointercancel", info.fallback.end, true);
      info.fallback = null;
    }
    if (!elBound) {
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerup", onEnd);
      el.removeEventListener("pointercancel", onCancel);
    }
    try { el.releasePointerCapture(info.pointerId); } catch (err) {}
  }

  function onRoiPointerDown(e) {
    if (!state.slide) return;
    if (e.button !== 0) return; // 只有主键开启拖拽；右键/中键不启动（留给菜单/平移）
    if (dragInfo || rectDrawInfo) return; // 已有活动手势：其他指针不下场
    e.preventDefault(); e.stopPropagation();
    dragInfo = {
      pointerId: e.pointerId,
      handle: (function () {
        var t = e.target;
        return (t && t.dataset && t.dataset.handle) || "move";
      })(),
      startRoi: { x: state.roi.x, y: state.roi.y, w: state.roi.w, h: state.roi.h },
      startImg: viewer.viewport.viewerElementToImageCoordinates(
        new OpenSeadragon.Point(e.clientX - getViewerRect().left,
                                e.clientY - getViewerRect().top)),
    };
    viewer.setMouseNavEnabled(false);
    bindRectPointerSession(roiBox, dragInfo,
                           onRoiPointerMove, onRoiPointerUp, onRoiPointerCancel);
  }

  function onRoiPointerMove(e) {
    if (!dragInfo || e.pointerId !== dragInfo.pointerId) return;
    // 释放事件丢失（框外/浏览器外松开、捕获被夺）后只剩悬停 move：
    // 立即按取消收尾，绝不继续用无按键的移动改写几何。
    if (!e.buttons) { onRoiPointerCancel(e); return; }
    e.preventDefault(); e.stopPropagation();
    var rect = getViewerRect();
    var curImg = viewer.viewport.viewerElementToImageCoordinates(
      new OpenSeadragon.Point(e.clientX - rect.left, e.clientY - rect.top));
    var s = dragInfo.startRoi;
    var handle = dragInfo.handle;
    var W = state.slide.width, H = state.slide.height;
    var nx = s.x, ny = s.y, nw = s.w, nh = s.h;
    if (handle === "move") {
      // 平移：保大小，clamp 到边界（到边缘保留大小）
      var dx = Math.round(curImg.x - dragInfo.startImg.x);
      var dy = Math.round(curImg.y - dragInfo.startImg.y);
      nx = clamp(s.x + dx, 0, Math.max(0, W - s.w));
      ny = clamp(s.y + dy, 0, Math.max(0, H - s.h));
    } else {
      // 边/角：锚定对边/对角，跟随指针（负方向随后归一，§6.2）
      var anchorX = s.x + s.w, anchorY = s.y + s.h; // 默认锚右/下
      var moveL = handle.indexOf("l") >= 0;
      var moveR = handle.indexOf("r") >= 0;
      var moveT = handle.indexOf("t") >= 0;
      var moveB = handle.indexOf("b") >= 0;
      if (moveL) anchorX = s.x + s.w;
      else if (moveR) anchorX = s.x;
      if (moveT) anchorY = s.y + s.h;
      else if (moveB) anchorY = s.y;
      var x0 = moveL || moveR ? anchorX : s.x;
      var x1 = moveL || moveR ? curImg.x : s.x + s.w;
      var y0 = moveT || moveB ? anchorY : s.y;
      var y1 = moveT || moveB ? curImg.y : s.y + s.h;
      var n = normalizeRect(x0, y0, x1, y1);
      if (!n) return; // 非法（NaN）保持上帧
      nx = n.x; ny = n.y; nw = n.w; nh = n.h;
    }
    state.roi.x = nx; state.roi.y = ny; state.roi.w = nw; state.roi.h = nh;
    viewer.updateOverlay(
      roiBox,
      viewer.viewport.imageToViewportRectangle(nx, ny, nw, nh),
      OpenSeadragon.Placement.TOP_LEFT
    );
    syncRoiSettings();
  }

  function onRoiPointerUp(e) {
    if (!dragInfo || e.pointerId !== dragInfo.pointerId) return;
    e.preventDefault(); e.stopPropagation();
    var info = dragInfo;
    dragInfo = null; // 先清状态：随后的释放捕获/丢失捕获不得回滚已完成的几何
    unbindRectPointerSession(roiBox, info,
                             onRoiPointerMove, onRoiPointerUp, onRoiPointerCancel);
    updateRoiOverlay(); // 完成态刷新：设置区/摘要/就近浮条与最终几何对齐
    viewer.setMouseNavEnabled(true);
  }

  // 工单 D：拖拽被打断（pointercancel / lostpointercapture / blur / 工具退出）
  // → 恢复拖前几何，不提交位移。幂等：无活动会话或事件属于其他指针时直接返回。
  function onRoiPointerCancel(e) {
    if (!dragInfo) return;
    if (e && e.pointerId != null && e.pointerId !== dragInfo.pointerId) return;
    if (e && e.preventDefault) { try { e.preventDefault(); } catch (err) {} }
    var info = dragInfo;
    dragInfo = null; // 先清状态再释放捕获（§3.2 顺序要求）
    unbindRectPointerSession(roiBox, info,
                             onRoiPointerMove, onRoiPointerUp, onRoiPointerCancel);
    var s = info.startRoi;
    state.roi.x = s.x; state.roi.y = s.y; state.roi.w = s.w; state.roi.h = s.h;
    updateRoiOverlay(); // 恢复拖前几何（含设置区/摘要/浮条重定位；无框时安全返回）
    viewer.setMouseNavEnabled(true);
  }

  // 矩形两类手势（拖框/画框）统一收尾：工具退出、Escape 撤稿、换片、窗口失焦
  // 都经这里；无活动会话时是 no-op，可安全重复调用。
  function cancelRectPointerSessions() {
    onRoiPointerCancel();
    onRectCanvasPointerCancel();
  }

  function getViewerRect() { return viewer.container.getBoundingClientRect(); }

  // ---------- 画布层拖出矩形 / 点选中心放置（矩形工具激活时） ----------
  // 工单 D（§5）：自由矩形 = 拖动完成即自动保存（QuPath 习惯）；预设尺寸矩形
  // = 点击中心放置后由 Enter/「保存标记」提交。两种模式用「屏幕像素」位移阈值
  // 区分（CSS px，与缩放倍率无关——旧的图像像素阈值在高倍率下会把轻抖动放大
  // 成有效拖动）。右键（button===2）不启动绘制（留给右键菜单工单 E）。
  var rectDrawInfo = null;
  var RECT_DRAG_SCREEN_PX = 4; // 判定「拖动」的屏幕位移阈值（CSS px）

  function onRectCanvasPointerDown(e) {
    if (!rectToolActive() || !state.slide) return false;
    if (e.button !== 0) return false; // 右键/中键不启动绘制（右键归工单 E 菜单）
    if (rectDrawInfo || dragInfo) return false; // 已有活动手势：其他指针不下场
    e.preventDefault(); e.stopPropagation();
    // 新的画布交互 = 放弃失败草稿的重试（按钮/Enter 重试在此之前仍可用），
    // 否则新拖出的几何会被旧 retryDraft 顶替提交。
    if (retryDraft && retryDraft.kind === "rect") {
      retryDraft = null;
      setDrawUnsaved(false);
    }
    var c = els.annoCanvas;
    var img0 = screenToImg(e);
    rectDrawInfo = {
      pointerId: e.pointerId,
      x0: img0.x, y0: img0.y, x1: img0.x, y1: img0.y,
      sx0: e.clientX, sy0: e.clientY,   // 屏幕起点（阈值判定用）
      startRoi: { x: state.roi.x, y: state.roi.y, w: state.roi.w, h: state.roi.h },
      moved: false,
    };
    viewer.setMouseNavEnabled(false);
    setDrawPhase("drawing");
    bindRectPointerSession(c, rectDrawInfo,
                           onRectCanvasPointerMove, onRectCanvasPointerUp,
                           onRectCanvasPointerCancel, true);
    return true;
  }

  function onRectCanvasPointerMove(e) {
    if (!rectDrawInfo || e.pointerId !== rectDrawInfo.pointerId) return false;
    // 释放事件丢失 → 后续只剩悬停 move：终止陈旧手势，不走完成/保存路径。
    if (!e.buttons) return onRectCanvasPointerCancel(e);
    e.preventDefault(); e.stopPropagation();
    // 屏幕（CSS）像素阈值：拖动判定与缩放倍率解耦
    if (Math.hypot(e.clientX - rectDrawInfo.sx0, e.clientY - rectDrawInfo.sy0)
        > RECT_DRAG_SCREEN_PX) {
      rectDrawInfo.moved = true;
    }
    var img = screenToImg(e);
    rectDrawInfo.x1 = img.x; rectDrawInfo.y1 = img.y;
    if (rectDrawInfo.moved) {
      var x1 = img.x, y1 = img.y;
      // Shift 约束正方形（QuPath 习惯）：按较大位移轴取边长，方向保留
      if (e.shiftKey) {
        var dx = x1 - rectDrawInfo.x0, dy = y1 - rectDrawInfo.y0;
        var side = Math.max(Math.abs(dx), Math.abs(dy));
        x1 = rectDrawInfo.x0 + (dx < 0 ? -side : side);
        y1 = rectDrawInfo.y0 + (dy < 0 ? -side : side);
      }
      var n = normalizeRect(rectDrawInfo.x0, rectDrawInfo.y0, x1, y1);
      if (n) {
        state.roi = n;
        createRoiBox();
        updateRoiOverlay();
      }
    }
    return true;
  }

  function onRectCanvasPointerUp(e) {
    if (!rectDrawInfo || e.pointerId !== rectDrawInfo.pointerId) return false;
    e.preventDefault(); e.stopPropagation();
    var c = els.annoCanvas;
    var info = rectDrawInfo;
    rectDrawInfo = null; // 先清状态：随后的释放捕获/丢失捕获不得再进入完成路径
    unbindRectPointerSession(c, info,
                             onRectCanvasPointerMove, onRectCanvasPointerUp,
                             onRectCanvasPointerCancel, true);
    setDrawPhase("idle");
    if (!info.moved) {
      // 点击放置（预设尺寸模式）：需要设置区已给出有效宽/高
      // （§6.1「先输入大小，再点击中心放置」）；放置后不自动保存，
      // 由 Enter / 「保存标记」提交。
      var px = rectInputsToPx();
      if (!px) {
        var unit = state.roiUnit;
        if (unit !== "px" && !unitToPxFactors(unit)) { toast(t("roi.need.mpp"), "error"); }
        else { toast(t("roi.input.invalid"), "error"); }
        viewer.setMouseNavEnabled(true);
        return true;
      }
      px = lockRatioAdjust(px.w, px.h);
      if (px.w > state.slide.width || px.h > state.slide.height) {
        toast(t("roi.input.invalid"), "error");
        viewer.setMouseNavEnabled(true);
        return true;
      }
      var img = screenToImg(e);
      placeRectAtCenter(img.x, img.y, px.w, px.h);
      createRoiBox();
      updateRoiOverlay();
      if (state.roi.w > 0) {
        els.saveBtn.disabled = false;
        els.saveAnnoBtn.disabled = false;
      }
      viewer.setMouseNavEnabled(true);
      return true;
    }
    // 自由矩形拖动完成：自动保存（与箭头一致），不再要求二次点击「保存标记」
    if (state.roi.w > 0) { submitCurrentDraft(); }
    viewer.setMouseNavEnabled(true);
    return true;
  }

  // 工单 D：拖动被打断（pointercancel / lostpointercapture / blur / Escape / 换片）
  // → 取消并恢复拖前选区，绝不走完成/保存路径。
  // 幂等：无活动会话或事件属于其他指针时直接返回。
  function onRectCanvasPointerCancel(e) {
    if (!rectDrawInfo) return false;
    if (e && e.pointerId != null && e.pointerId !== rectDrawInfo.pointerId) return false;
    if (e && e.preventDefault) { try { e.preventDefault(); } catch (err) {} }
    var c = els.annoCanvas;
    var info = rectDrawInfo;
    rectDrawInfo = null; // 先清状态再释放捕获（§3.2 顺序要求）
    unbindRectPointerSession(c, info,
                             onRectCanvasPointerMove, onRectCanvasPointerUp,
                             onRectCanvasPointerCancel, true);
    // 恢复拖前选区（工具刚激活时为空选区）
    if (info.startRoi) {
      state.roi.x = info.startRoi.x; state.roi.y = info.startRoi.y;
      state.roi.w = info.startRoi.w; state.roi.h = info.startRoi.h;
    }
    if (!(state.roi.w > 0) && roiBox) {
      try { if (viewer && viewer.currentOverlays) viewer.removeOverlay(roiBox); } catch (err) {}
      if (roiBox.parentNode) roiBox.parentNode.removeChild(roiBox);
      roiBox = null;
      closeAnnoToolbar(); // 框已移除：就近浮条不能留着无主漂浮
    } else {
      updateRoiOverlay();
    }
    viewer.setMouseNavEnabled(true);
    setDrawPhase("idle");
    return true;
  }

  // Escape 取消未保存选区（§6.1）：恢复 viewer 导航；
  // Enter 提交有效未提交草稿（工单 D：预设放置/失败重试）。
  function onRectKeydown(e) {
    if (e.key === "Escape") {
      if (rectDrawInfo) {
        onRectCanvasPointerCancel(e);
        return;
      }
      if (rectToolActive()) {
        e.preventDefault();
        // 有未提交草稿（放置/失败重试）→ 先撤草稿；再次 Escape 才退出工具
        if (discardRectDraft()) {
          toast(t("draw.cancelled"), "info");
          return;
        }
        exitRoi();
        toast(t("roi.cancelled"), "info");
      }
      return;
    }
    if (e.key === "Enter") {
      if (rectDrawInfo || dragInfo) return; // 拖动中不提交（画框与拖框都算）
      if (!rectToolActive()) return;
      e.preventDefault();
      submitCurrentDraft();
    }
  }

  // 撤销当前未提交矩形草稿（选区/失败重试态）：清选区、留在工具内。
  // 返回是否真的撤掉了东西。
  function discardRectDraft() {
    cancelRectPointerSessions(); // 撤稿 = 活动手势一并收尾（拖框/画框；幂等）
    var had = !!(retryDraft && retryDraft.kind === "rect") || state.roi.w > 0;
    if (!had) return false;
    retryDraft = null;
    state.roi = { x: 0, y: 0, w: 0, h: 0 };
    if (roiBox) {
      try { if (viewer && viewer.currentOverlays) viewer.removeOverlay(roiBox); } catch (err) {}
      if (roiBox.parentNode) roiBox.parentNode.removeChild(roiBox);
      roiBox = null;
      closeAnnoToolbar(); // 框已移除：就近浮条不能留着无主漂浮
    }
    if (els.saveBtn) els.saveBtn.disabled = true;
    if (els.saveAnnoBtn) els.saveAnnoBtn.disabled = true;
    setDrawUnsaved(false);
    syncRoiSettings();
    updateRoiSummary();
    return true;
  }

  // 已有矩形标注的 w/h 读取（升级 C：v2 w/h 权威；旧 side_px 正方形兼容）。
  // 不重新正方形化：非正方形取各自轴，绝不 max/min 冒充。
  function rectItemW(it) {
    var w = Number(it.w);
    if (isFinite(w) && w > 0) return w;
    return Number(it.side_px) > 0 ? Number(it.side_px) : 0;
  }
  function rectItemH(it) {
    var h = Number(it.h);
    if (isFinite(h) && h > 0) return h;
    return Number(it.side_px) > 0 ? Number(it.side_px) : 0;
  }

  // 标注的物理尺寸展示（分轴反算；AI 落标 size_mm 常为 0 → 用 mpp 现算）
  function rectItemSizeText(it) {
    var w = rectItemW(it), h = rectItemH(it);
    if (!(w > 0) || !(h > 0)) return "";
    var mx = Number(state.mppX), my = Number(state.slide && state.slide.mppY);
    if (!posNum(mx) || !posNum(my)) {
      var mm0 = Number(it.size_mm);
      return (mm0 > 0 && w === h) ? (mm0 + "mm") : "";
    }
    var wx = w * mx, hy = h * my; // µm
    return (wx / 1000).toFixed(2) + "×" + (hy / 1000).toFixed(2) + "mm";
  }

  // ---------- 导出标注（QuPath GeoJSON） ----------
  // 导出当前用户在当前切片上可见的标注：服务端与 /api/annotations 列表
  // 端点共用同一读门禁与标注级过滤，导出不会放大可见范围。
  var annoExportBusy = false;
  // 单条导出：按 annotation_id 记忙碌，重复点击（含重渲染前的旧按钮）忽略
  var annoExportOneBusy = {};

  function annoExportStem() {
    var s = state.slide || {};
    var src = String(s.original_filename || s.display_name || s.name || "");
    var base = src.replace(/\\/g, "/").split("/").pop()
      .replace(/\.[^.]+$/, "")
      .replace(/[\u0000-\u001f\u007f]/g, "")
      .trim();
    return base || (s.id ? "slide_" + String(s.id) : "slide");
  }

  // 解析 Content-Disposition；没有可用文件名时返回 null（调用方决定回退名）
  function _annoExportFilename(disposition) {
    if (disposition) {
      // RFC 5987 filename*（中文名走该形态）；解码失败回落默认名
      var m = /filename\*\s*=\s*utf-8''([^;]+)/i.exec(disposition);
      if (m) {
        try {
          return decodeURIComponent(m[1].trim().replace(/^"|"$/g, ""));
        } catch (e) { /* fall through */ }
      }
      m = /filename\s*=\s*"([^"]*)"/i.exec(disposition) ||
        /filename\s*=\s*([^;]+)/i.exec(disposition);
      if (m && m[1].trim()) return m[1].trim();
    }
    return null;
  }

  function annoExportFilename(disposition) {
    return _annoExportFilename(disposition) ||
      (annoExportStem() + "_annotations.geojson");
  }

  function annoExportError(res) {
    return res.json().catch(function () { return {}; }).then(function (j) {
      throw new Error((j && j.error) || ("HTTP " + res.status));
    });
  }

  // 头部整片导出与行内单条导出共用的下载步骤（blob → 临时 <a download>）
  function downloadAnnoExport(res, fname) {
    return res.blob().then(function (blob) {
      var a = document.createElement("a");
      var objUrl = URL.createObjectURL(blob);
      a.href = objUrl;
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(objUrl); }, 1000);
    });
  }

  function syncAnnoExportBtn() {
    if (!els.annoExportBtn) return;
    var ready = !!(slideIdApiOn() && state.slide && state.slide.id);
    els.annoExportBtn.disabled = !ready || annoExportBusy;
  }

  function exportAnnotations() {
    if (annoExportBusy) return;
    var sid = (slideIdApiOn() && state.slide && state.slide.id) || "";
    if (!sid) { syncAnnoExportBtn(); return; }
    annoExportBusy = true;
    syncAnnoExportBtn();
    return apiFetch("/api/slides/" + encodeURIComponent(sid) + "/annotations/export")
      .then(function (res) {
        if (!res.ok) return annoExportError(res);
        var n = parseInt(res.headers.get("X-Annotation-Count") || "0", 10);
        if (!(n > 0)) {
          toast(t("anno.export.empty"), "info");
          return null;
        }
        var fname = annoExportFilename(res.headers.get("Content-Disposition"));
        return downloadAnnoExport(res, fname).then(function () {
          toast(t("anno.export.done", { n: n }), "success");
        });
      })
      .catch(function (e) {
        toast(t("anno.export.fail", { s: (e && e.message) || "?" }), "error");
      })
      .finally(function () {
        annoExportBusy = false;
        syncAnnoExportBtn();
      });
  }

  // 导出单条标注（annotation_id 参数）；文件名优先取响应头，回退
  // <切片 stem>_<标注名>.geojson。
  function annoExportOne(it, label, btn) {
    var aid = it && it.annotation_id;
    if (!aid || annoExportOneBusy[aid]) return;
    var sid = (slideIdApiOn() && state.slide && state.slide.id) || "";
    if (!sid) return;
    annoExportOneBusy[aid] = true;
    if (btn) btn.disabled = true;
    var fallback = annoExportStem() + (label ? "_" + label : "") + ".geojson";
    return apiFetch("/api/slides/" + encodeURIComponent(sid) +
        "/annotations/export?annotation_id=" + encodeURIComponent(aid))
      .then(function (res) {
        if (!res.ok) return annoExportError(res);
        var fname = _annoExportFilename(
          res.headers.get("Content-Disposition")) || fallback;
        return downloadAnnoExport(res, fname).then(function () {
          toast(t("anno.export.one.done",
            { name: truncateMiddle(label, 24) }), "success");
        });
      })
      .catch(function (e) {
        toast(t("anno.export.fail", { s: (e && e.message) || "?" }), "error");
      })
      .finally(function () {
        delete annoExportOneBusy[aid];
        if (btn) btn.disabled = false;
      });
  }

  // ---------- 保存图片（裁剪） ----------
  function saveCrop() {
    if (!state.slide || !rectToolActive()) return;
    var r = state.roi;
    // P2 合同 §4：crop 走 /api/slides/<id>/crop（ID 通道）；旧后端按名
    var ref = state.slide.id || state.slide.name;
    // Batch 4（§4.4）：多通道切片 crop 与屏幕瓦片同 render_token（服务端用同
    // 一 context 合成，且像素预算按启用通道数计）；RGB/legacy 不带参数
    var token = (channelCtrl && channelCtrl.isMultichannel())
      ? channelCtrl.getToken() : null;
    var url = slideCropUrl(ref, r, token);
    var fp8 = ((channelCtrl && channelCtrl.getFingerprint()) || "").slice(0, 8);
    var multi = !!(channelCtrl && channelCtrl.isMultichannel() && token);
    var originalText = els.saveBtn.textContent;
    els.saveBtn.textContent = t("export.busy");
    els.saveBtn.disabled = true;
    apiFetch(url)
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(function (j) {
            throw new Error(j.error || (t("export.fail") + " " + res.status));
          });
        }
        return res.blob();
      })
      .then(function (blob) {
        var stem = (state.slide.original_filename || state.slide.name)
          .replace(/\.[^.]+$/, "");
        // 下载文件名与后端 Content-Disposition 同形：含宽高 + 多通道 fp 前 8 位
        var fname = stem + "_x" + Math.round(r.x) + "_y" + Math.round(r.y) +
          "_" + Math.round(r.w) + "x" + Math.round(r.h) + "px" +
          (multi && fp8 ? "_fp" + fp8 : "") + ".png";
        var a = document.createElement("a");
        var objUrl = URL.createObjectURL(blob);
        a.href = objUrl; a.download = fname;
        document.body.appendChild(a); a.click();
        document.body.removeChild(a);
        setTimeout(function () { URL.revokeObjectURL(objUrl); }, 1000);
        // 确认文案明确「导出当前伪彩合成图」，不冒充原始科学数据（§3.2/§4.4）
        toast(multi
          ? t("export.pseudo.done", { name: fname })
          : t("export.done", { name: fname }), "success");
      })
      .catch(function (e) { toast(t("export.fail2", { s: e.message }), "error"); })
      .finally(function () {
        els.saveBtn.textContent = originalText;
        els.saveBtn.disabled = !rectToolActive();
      });
  }

  // ---------- 保存矩形选区为标注（管理员 rect 标注，v2 成对 w/h） ----------
  // 工单 D：与拖动完成的自动保存共用一条提交路径（幂等键 + 草稿保留 +
  // 成功后选中新标注）。「保存图片」（saveCrop，裁剪导出）保持独立，
  // 不因本函数产生标注。
  function saveAnno() {
    if (!state.slide || !rectToolActive()) return;
    if (!(state.roi.w > 0)) return;
    submitCurrentDraft();
  }

  // ---------- 手动设置 mpp（等轴校准：显式操作，来源标记 manual） ----------
  function setMpp() {
    var v = parseFloat(els.mppInput.value);
    if (!isFinite(v) || v <= 0) { toast(t("mpp.invalid"), "error"); return; }
    state.mppX = v;
    if (state.slide) {
      state.slide.mppX = v;
      // 升级 C（§6.2）：单一输入的等轴校准是显式选择——两轴同值并标 manual
      state.slide.mppY = v;
      state.slide.mppSource = "manual";
    }
    // 升级 C（§6.2）：MPP/校准更新不重写已有标注（及当前选区）的像素范围，
    // 只更新物理显示——refresh 显示层即可。
    updateRoiOverlay();
    updateMppSetterVisibility();
    // Wave 3：显式手动校准后（前端态，非持久化），物理单位重新可用
    syncUnitAvailability();
    toast(t("mpp.set.ok", { v: v }), "success");
  }

  // =========================================================================
  // 项目渲染与管理（改版 2026-10-08：项目 = 文件夹，见下方文件夹/切片浏览器）
  // =========================================================================
  function loadAll() {
    // 并行加载切片、项目、分享、标注索引（AI 配置由 HistoPilot 插件 bundle 自行加载）
    return Promise.all([
      fetch("/api/slides").then(function (r) { return r.json(); }),
      fetch("/api/projects").then(function (r) { return r.json(); }),
      fetch("/api/share/list").then(function (r) { return r.json(); }),
      loadAnnotationsIndex(),
    ]).then(function (results) {
      allSlides = results[0] || [];
      allProjects = results[1] || [];
      renderFolderBrowser();
      rearmTempViewTimerFromList();
      renderShareList(shareListFrom(results[2]));
      // folder-experience §2：授权项目列表就绪后按记忆恢复（仅初始化一次；
      // 深链/本次会话导航优先，见 fbRestoreFromMemory）
      fbRestoreFromMemory();
    }).catch(function (e) {
      toast(t("load.fail", { e: e }), "error");
    });
  }

  // 重载项目+切片并重渲文件夹浏览器（分享/标注不变）。名字里的 unfiled 是
  // 历史口径（旧侧栏「未归类」分区），现统一渲染为根目录内容。
  function reloadProjectsAndUnfiled() {
    return Promise.all([
      fetch("/api/projects").then(function (r) { return r.json(); }),
      fetch("/api/slides").then(function (r) { return r.json(); }),
      loadAnnotationsIndex(),
    ]).then(function (results) {
      allProjects = results[0] || [];
      allSlides = results[1] || [];
      renderFolderBrowser();
      rearmTempViewTimerFromList();
    });
  }

  // DEF-5（2026-10-08 集成验收）：/api/share/list 现返回裸数组，旧后端/契约
  // 返回 {shares:[...]}——两种形状都接受，避免列表恒为「暂无分享」。
  function shareListFrom(data) {
    if (Array.isArray(data)) return data;
    return (data && data.shares) || [];
  }

  function reloadShares() {
    return apiFetch("/api/share/list")
      .then(function (r) { return r.json(); })
      .then(function (data) { renderShareList(shareListFrom(data)); });
  }

  // 渲染单个切片信息块（用于项目行、未归类项、选择器项）
  // 行式版：纯文本 "宽×高 · mpp x.xx"，估算值带 *
  function slideMetaTags(s) {
    var parts = [];
    if (s.width && s.height) {
      parts.push(s.width + "×" + s.height);
    }
    if (s.mpp_x != null) {
      // mpp 保留 3 位小数，避免副行过长被截断
      var mpp = Math.round(s.mpp_x * 1000) / 1000;
      parts.push("mpp " + mpp + (s.mpp_source === "estimated" ? "*" : ""));
    } else {
      // Wave 3（普通图片兼容）：产品语义说「无物理标尺」，不说「mpp 缺失」
      // （缺 mpp ≠ 数据缺陷；普通图片本就不携带物理标尺）
      parts.push(tt("slide.meta.no.scale"));
    }
    return parts.join(" · ");
  }

  // =========================================================================
  // 文件夹/切片浏览器（改版 2026-10-08，admin-viewer-simplified §5.1–5.3）
  // -------------------------------------------------------------------------
  // 项目（project）在 UI 文案中叫「文件夹」；层级由 parent_project_id 承载
  // （本分支后端尚未合入：字段缺失一律视为根目录）。结构：
  //   根目录 = 顶层文件夹 + 「临时查看」虚拟文件夹（仅有临时授权切片时）+ 未归类切片
  //   文件夹 = 子文件夹卡片 + 该文件夹的切片卡片堆叠
  // 堆叠：每叠最多 FB_MAX_PAGE 张、按可用高度缩减（文件夹卡占用同一预算）；
  // 纵向重叠、按鼠标位置连续展开相邻卡（减弱动态效果时取消插值和旋转）；
  // 翻页只换列表不动 Viewer；页码按文件夹记在内存（fbState.pages，刷新不保留）。
  // =========================================================================
  var FB_TEMP_KEY = "__temp__";   // 「临时查看」虚拟文件夹键（不对应项目）
  var FB_MAX_PAGE = 8;            // 每叠最多 8 张（§5.2）
  var FB_GAP = 44;                // 堆叠露出条带的偏好高度（名称条+一段缩略图）
  var FB_MIN_GAP = 44;            // 条带下限（页容量按最紧条带计，保证放得下）
  var FB_MAX_GAP = 128;           // 条带上限 = 整卡高（短叠铺开时不再过度拉伸）
  var FB_CARD_H = 128;            // 完整卡片高度
  var FB_PULL_PX = 72;            // 悬停时卡片从叠中横向抽出的距离
  var FB_FOLDER_H = 64;           // 文件夹卡高度
  var FB_FOLDER_GAP = 8;          // 文件夹卡间距
  var FB_STACK_FALLBACK_H = 396;  // 无布局环境（测试/首帧）时的可用高度兜底

  var fbState = {
    folder: null,   // null=根目录；pid=项目 id；FB_TEMP_KEY=临时查看
    pages: {},      // 文件夹 → 上次页码（会话内存 + 位置记忆持久化）
    page: 0,        // 当前文件夹当前页（渲染时钳回有效范围）
    totalPages: 1,
  };

  // =========================================================================
  // 位置记忆（folder-experience 2026-10-13 §2）：小型版本化 localStorage 记录
  // {"v":1,"folder":id|null,"pages":{...},"updatedAt":…}，按真实登录身份隔离
  // （currentUserId=预览中的有效被预览者；actor 不入 key——侧栏呈现的是有效
  // 身份的授权树）。只存 ID、页码与时间，绝不存名称/缩略图/切片清单/token；
  // 上限 100 个目录、30 天。storage 抛错/隐私模式 → 纯内存继续，不阻断导航
  // 与上传。恢复只在初始化尝试一次，且晚于身份与授权项目列表；两个部署域
  // 各自存储，不做跨域同步。
  // =========================================================================
  var FB_MEM_KEY_PREFIX = "pt.fb.pos.v1:";
  var FB_MEM_MAX_FOLDERS = 100;
  var FB_MEM_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
  var FB_ASSOC_KEY_PREFIX = "pt.fb.assoc.v1:";   // 关联失败待办（§1.4，同身份隔离）
  var FB_ASSOC_MAX_ITEMS = 20;
  var fbMemRecord = null;        // 内存兜底记录（storage 不可用时仍可会话内恢复）
  var fbMemRestoreDone = false;  // 记忆恢复只在初始化尝试一次
  var fbNavTouched = false;      // 本次会话已发生导航（优先级高于磁盘记忆）
  // 身份就绪闸：initAuth 结算后放行恢复。先到先结算——身份先于项目列表到位
  // 时闸尚未创建，也要记住「已就绪」（fbIdentitySettle），否则无人再放行。
  var fbIdentitySettled = false;
  var fbIdentityWaiters = [];

  function fbIdentityReady() {
    if (fbIdentitySettled) return Promise.resolve();
    return new Promise(function (res) { fbIdentityWaiters.push(res); });
  }

  function fbIdentitySettle() {
    if (fbIdentitySettled) return;
    fbIdentitySettled = true;
    fbIdentityWaiters.splice(0).forEach(function (res) { res(); });
  }

  // 记忆 key：无可信身份（未登录/身份未就绪）→ null，不读写磁盘。
  function fbMemoryKey() {
    if (currentUserId == null || currentUserId === "") return null;
    return FB_MEM_KEY_PREFIX + String(currentUserId);
  }

  // 读取 + 校验 + 裁剪：只认 {v:1,…} 形状；过期（30 天）整体丢弃；目录数
  // 截到上限；页码只收非负有限数。「临时查看」是会话级集合，永不恢复。
  function fbMemoryLoad(store) {
    var key = fbMemoryKey();
    if (!key || !store) return null;
    var raw = null;
    try { raw = store.getItem(key); } catch (e) { return null; }
    if (!raw) return null;
    var rec = null;
    try { rec = JSON.parse(raw); } catch (e) { return null; }
    if (!rec || typeof rec !== "object" || rec.v !== 1) return null;
    var updatedAt = (typeof rec.updatedAt === "number" && isFinite(rec.updatedAt))
      ? rec.updatedAt : 0;
    if (Date.now() - updatedAt > FB_MEM_MAX_AGE_MS) return null;
    var pages = {};
    if (rec.pages && typeof rec.pages === "object") {
      Object.keys(rec.pages).slice(0, FB_MEM_MAX_FOLDERS).forEach(function (k) {
        var n = rec.pages[k];
        if (typeof n === "number" && isFinite(n) && n >= 0) pages[k] = Math.floor(n);
      });
    }
    var folder = rec.folder ? String(rec.folder) : null;
    if (folder === FB_TEMP_KEY) folder = null;
    var anchor = (typeof rec.anchor === "string" && rec.anchor.length <= 200)
      ? rec.anchor : null;
    return { v: 1, folder: folder, pages: pages, anchor: anchor,
             updatedAt: updatedAt };
  }

  // 写入：导航/翻页结束调用。临时查看目录按根记录（刷新后回根，不恢复会话
  // 集合）；storage 抛错仅失去持久化（fbMemRecord 留作会话内兜底）。
  function fbMemorySave() {
    if (fbMemoryKey() == null) return;   // 无可信身份不持久化
    fbState.pages[fbState.folder] = fbState.page;
    var pages = {};
    Object.keys(fbState.pages).slice(0, FB_MEM_MAX_FOLDERS).forEach(function (k) {
      pages[k] = fbState.pages[k];
    });
    var rec = {
      v: 1,
      folder: fbState.folder === FB_TEMP_KEY ? null : (fbState.folder || null),
      pages: pages,
      // 当前页首条：页码随侧栏可用高度变化，恢复以它所在页为准
      anchor: fbState.folder === FB_TEMP_KEY ? null : (fbState.anchor || null),
      updatedAt: Date.now(),
    };
    fbMemRecord = rec;
    var store = safeLocalStorage();
    if (!store) return;
    try { store.setItem(fbMemoryKey(), JSON.stringify(rec)); }
    catch (e) { /* 隐私模式/配额：仅失去刷新恢复，导航照常 */ }
  }

  // URL ?slide= 深链接检测（与 openSlideFromLocation 同一正则口径）：深链
  // 优先于磁盘记忆——侧栏由 fbLocateSlide 定位，不被旧记忆拉回。
  function fbHasSlideDeepLink() {
    try { return /[?&]slide=([^&]+)/.test(window.location.search || ""); }
    catch (e) { return false; }
  }

  // 初始化恢复（仅一次）：身份与授权项目列表都就绪后，按记忆恢复当前目录
  // 与页码。优先级：URL ?slide= 深链 / 本次搜索定位 > 本次会话内导航 > 磁盘
  // 记忆。记忆目录已删除/已撤权 → 回根目录：只与本地 allProjects（最新授权
  // 列表）比对，绝不发起对未授权目录的内容请求。页码交给 renderFolderBrowser
  // 按 fbPackPages 实际页数钳回（目录内容变少时落在最后一个有效页）。
  function fbRestoreFromMemory() {
    if (fbMemRestoreDone) return;
    fbMemRestoreDone = true;
    // 关联失败待办（§1.4）同闸重试：沿用原 slide ID/回执与原目标快照
    fbIdentityReady().then(function () {
      fbAssocRetryPending();
      if (fbHasSlideDeepLink()) return;   // 深链优先：fbLocateSlide 负责侧栏
      if (fbNavTouched) return;           // 本次会话用户已导航：不回拉旧记忆
      var store = safeLocalStorage();
      var rec = fbMemRecord || (store ? fbMemoryLoad(store) : null);
      if (!rec) return;
      var pages = rec.pages || {};
      Object.keys(pages).forEach(function (k) {
        if (fbState.pages[k] == null) fbState.pages[k] = pages[k];
      });
      var folder = rec.folder || null;
      if (folder && !fbProject(folder)) folder = null;   // 已删/撤权 → 根
      fbState.folder = folder;
      fbState.page = (typeof pages[folder] === "number") ? pages[folder] : 0;
      var anchorKnown = !!rec.anchor && fbOrderedEntries(folder).some(function (it) {
        return fbEntryKey(it) === rec.anchor;
      });
      if (anchorKnown) {
        fbState.anchor = rec.anchor;
        renderFolderBrowser(true);   // 按记忆首条所在页定位
      } else {
        renderFolderBrowser();   // 页码在此按实际页数钳回
      }
    });
  }

  // ---------- 关联失败待办（folder-experience §1.4）----------
  // 只存 slide ID/名 + 目标快照（pid/幂等键），同身份隔离；成功关联即清除。
  // 换账号绝不读取上一账号的待办；storage 不可用退化为仅行内重试按钮。
  function fbAssocKey() {
    if (currentUserId == null || currentUserId === "") return null;
    return FB_ASSOC_KEY_PREFIX + String(currentUserId);
  }

  function fbAssocRead() {
    var key = fbAssocKey();
    var store = safeLocalStorage();
    if (!key || !store) return [];
    try {
      var arr = JSON.parse(store.getItem(key) || "[]");
      if (!Array.isArray(arr)) return [];
      return arr.filter(function (it) {
        return it && (it.slideId || it.slideName) && it.target &&
          (it.target.pid || it.target.newProjectName);
      }).slice(0, FB_ASSOC_MAX_ITEMS);
    } catch (e) { return []; }
  }

  function fbAssocWrite(items) {
    var key = fbAssocKey();
    var store = safeLocalStorage();
    if (!key || !store) return;
    try { store.setItem(key, JSON.stringify(items)); } catch (e) { /* 同上：不阻断 */ }
  }

  function fbAssocPendingSave(slideId, slideName, target) {
    var items = fbAssocRead().filter(function (it) {
      return !(it.slideId === slideId && it.slideName === slideName);
    });
    items.push({
      slideId: slideId || null,
      slideName: slideName || null,
      target: {
        pid: target.pid || "",
        newProjectName: target.newProjectName || "",
        createdPid: target.createdPid || "",
        createKey: target.createKey || null,   // 原幂等键：重试绝不重复建目录
      },
      at: Date.now(),
    });
    fbAssocWrite(items.slice(-FB_ASSOC_MAX_ITEMS));
  }

  function fbAssocPendingClear(slideId, slideName) {
    fbAssocWrite(fbAssocRead().filter(function (it) {
      return !(it.slideId === slideId && it.slideName === slideName);
    }));
  }

  // 每次启动对待办自动重试一次（每次刷新至多一轮）：只重试原目标的关联
  // 请求，绝不重传已发布切片，也绝不改用当前目录。
  function fbAssocRetryPending() {
    fbAssocRead().forEach(function (it) {
      importAssociateUploaded(it.slideId, it.slideName, it.target, null);
    });
  }

  // ---------- 刷新恢复的上传目标快照（§1.3 × 刷新恢复 §5）----------
  // 上传创建 ingestion（created 事件）时，把该任务开始时冻结的目标快照按
  // **任务 ID**（非文件名）落盘，同身份隔离、只存 ID/幂等键、30 天过期。
  // 恢复行完成（pollRestoredRow → viewable）时用原快照走同一条
  // importAssociateUploaded 路径——失败进入上面的待办/重试机制；快照缺失/
  // 过期 → 不猜，保持未归类（与既有行为一致）。任务记录移除时一并清理。
  var FB_UPASSOC_KEY_PREFIX = "pt.fb.upassoc.v1:";
  var FB_UPASSOC_MAX_ITEMS = 20;

  function fbUpAssocKey() {
    if (currentUserId == null || currentUserId === "") return null;
    return FB_UPASSOC_KEY_PREFIX + String(currentUserId);
  }

  function fbUpAssocRead() {
    var key = fbUpAssocKey();
    var store = safeLocalStorage();
    if (!key || !store) return [];
    try {
      var arr = JSON.parse(store.getItem(key) || "[]");
      if (!Array.isArray(arr)) return [];
      var now = Date.now();
      return arr.filter(function (it) {
        return it && typeof it.job_id === "string" && it.job_id &&
          it.target && (it.target.pid || it.target.newProjectName) &&
          typeof it.at === "number" && isFinite(it.at) &&
          now - it.at <= FB_MEM_MAX_AGE_MS;
      }).slice(0, FB_UPASSOC_MAX_ITEMS);
    } catch (e) { return []; }
  }

  function fbUpAssocWrite(items) {
    var key = fbUpAssocKey();
    var store = safeLocalStorage();
    if (!key || !store) return;
    try { store.setItem(key, JSON.stringify(items)); } catch (e) { /* 不阻断 */ }
  }

  function fbUpAssocSave(jobId, target) {
    if (!jobId || !target ||
        (!target.pid && !target.newProjectName)) return;   // 未归类：无可恢复目标
    var items = fbUpAssocRead().filter(function (it) { return it.job_id !== jobId; });
    items.push({
      job_id: jobId,
      target: {
        pid: target.pid || "",
        newProjectName: target.newProjectName || "",
        createdPid: target.createdPid || "",
        createKey: target.createKey || null,   // 原幂等键：恢复完成不重复建目录
      },
      at: Date.now(),
    });
    fbUpAssocWrite(items.slice(-FB_UPASSOC_MAX_ITEMS));
  }

  // 取回并移除（读端一次性消费：关联恰好一次）
  function fbUpAssocTake(jobId) {
    if (!jobId) return null;
    var items = fbUpAssocRead();
    var hit = null;
    var rest = items.filter(function (it) {
      if (!hit && it.job_id === jobId) { hit = it; return false; }
      return true;
    });
    if (hit) fbUpAssocWrite(rest);
    return hit ? hit.target : null;
  }

  // 每叠页数：可用高度 − 文件夹卡占用 − 一张完整卡 → 余量按最紧露出条带
  // （FB_MIN_GAP）折算——保证整页在最挤时也放得下；铺开是渲染期的自适应。
  // 纯函数（vitest 锁行为）：上限 8、下限 1。（渲染/定位现改用下方
  // fbPackPages 按页装箱；本函数保留为单叠容量的口径基线与测试面。）
  function fbPageSize(availH, folderCount) {
    var used = (folderCount || 0) * (FB_FOLDER_H + FB_FOLDER_GAP);
    var remaining = (availH || 0) - used - FB_CARD_H;
    var n = Math.floor(remaining / FB_MIN_GAP) + 1;
    if (!isFinite(n)) n = FB_MAX_PAGE;
    return Math.max(1, Math.min(FB_MAX_PAGE, n));
  }

  // 有序条目（渲染与搜索定位共用同一顺序，§5.2）：文件夹卡在前、切片卡在后。
  function fbOrderedEntries(folderKey) {
    var e = fbEntries(folderKey);
    return e.folders.map(function (f) {
      return { kind: "folder", key: f.key, name: f.name, count: f.count, virtual: !!f.virtual };
    }).concat(e.slides.map(function (s) {
      return { kind: "slide", s: s, sid: slideRefOf(s), name: s.name || s.original_filename || slideRefOf(s) };
    }));
  }

  // 叠页装箱（§5.2）：按**本页实际条目**的真实高度装箱——文件夹卡 64+8、
  // 切片卡首张即整卡 128、之后每张再占一条露出条带（下限 44，最挤也放得下）。
  // 只计本页条目：不在本页的文件夹绝不摊派到每一页的预算（修「目录里 N 个
  // 文件夹把页容量压到 1 张」）。每页 ≤8、下限 1。返回页数组（渲染与搜索
  // 定位共用同一函数，页码/页内容恒一致）。
  function fbPackPages(items, availH) {
    var pages = [];
    var cur = [];
    var curH = 0;
    var slides = 0;
    function costOf(it) {
      return it.kind === "folder"
        ? FB_FOLDER_H + FB_FOLDER_GAP
        : (slides === 0 ? FB_CARD_H : FB_MIN_GAP);
    }
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var cost = costOf(it);
      if (cur.length >= FB_MAX_PAGE || (cur.length > 0 && curH + cost > availH)) {
        pages.push(cur);
        cur = [];
        curH = 0;
        slides = 0;
        cost = costOf(it); // 新页首张切片按整卡高计
      }
      cur.push(it);
      curH += cost;
      if (it.kind === "slide") slides += 1;
    }
    if (cur.length) pages.push(cur);
    return pages.length ? pages : [[]]; // 空目录也保持单页（页码恒 1/1）
  }

  // 露出条带高度：短叠按剩余高度铺开（P1：贴到翻页钮上方为止），上限=整卡
  // 高（不过度拉伸）、下限=FB_MIN_GAP（名称条保持可读）。纯函数。
  function fbBandGap(availH, folderCount, slideCount) {
    var used = (folderCount || 0) * (FB_FOLDER_H + FB_FOLDER_GAP);
    var remaining = (availH || 0) - used - FB_CARD_H;
    if (!(slideCount > 1)) return FB_GAP;
    var gap = Math.floor(remaining / (slideCount - 1));
    if (!isFinite(gap)) return FB_GAP;
    return Math.max(FB_MIN_GAP, Math.min(FB_MAX_GAP, gap));
  }

  function fbStackAvailH() {
    var el = els.fbStack;
    if (el && typeof el.clientHeight === "number" && el.clientHeight > 40) {
      return el.clientHeight;
    }
    return FB_STACK_FALLBACK_H;
  }

  // 当前装箱是否反映可见布局：桌面收起（含收起动画中）时侧栏宽度在变、
  // 高度仍在，装箱结果不代表展开后的分页
  function fbStackMeasured() {
    var el = els.fbStack;
    if (!el || typeof el.clientHeight !== "number" || el.clientHeight <= 40) return false;
    var sb = document.getElementById("sidebar");
    return !(sb && sb.classList && sb.classList.contains("collapsed"));
  }

  function fbProject(pid) {
    if (!pid) return null;
    for (var i = 0; i < allProjects.length; i++) {
      if (allProjects[i].pid === pid) return allProjects[i];
    }
    return null;
  }

  // 父项目：字段缺失/空 = 根目录（本分支后端未合入 parent_project_id 时的兼容口径）
  function fbParentOf(p) {
    return (p && p.parent_project_id) ? String(p.parent_project_id) : null;
  }

  function fbChildFolders(folderKey) {
    var want = folderKey || null;
    return allProjects.filter(function (p) { return fbParentOf(p) === want; });
  }

  // pid 是否位于 ancestorPid 的子树内（含自身）
  function fbInSubtree(pid, ancestorPid) {
    var p = fbProject(pid);
    while (p) {
      if (p.pid === ancestorPid) return true;
      p = fbProject(fbParentOf(p));
    }
    return false;
  }

  // 项目内切片（项目行 slide_refs 与 /api/slides 逐项对齐；旧后端 slides 名数组）
  function fbFolderProjectSlides(p) {
    var out = [];
    projectSlideRefs(p).forEach(function (ref) {
      var sinfo = ref.slide_id ? findSlideInfoById(ref.slide_id) : findSlideInfo(ref.slide);
      if (sinfo) out.push(sinfo);
      else if (ref.slide_id) out.push({ slide_id: ref.slide_id, name: ref.slide || null, __missing: true });
    });
    return out;
  }

  // 临时查看到期（或本地已结束标记）的切片不再出现在任何列表
  function fbTempSlides() {
    return allSlides.filter(function (s) {
      return s.temporary_view_expires_at && !s.__tempEnded;
    });
  }

  // 根目录未归类：不在任何项目中、非临时查看（§5.1）
  function fbUnfiledSlides() {
    return allSlides.filter(function (s) {
      return !isSlideInAnyProject(s) && !s.temporary_view_expires_at;
    });
  }

  // 文件夹卡计数：子树内切片总数（含子文件夹，原型口径「N 张切片」）
  function fbSubtreeCount(pid) {
    var n = fbFolderProjectSlides(fbProject(pid) || {}).length;
    fbChildFolders(pid).forEach(function (c) { n += fbSubtreeCount(c.pid); });
    return n;
  }

  // 位置路径（面包屑文本）：根 → 「我的切片」；子目录 → 「教学切片 / 复核」
  function fbPathOf(folderKey) {
    var names = [];
    var p = fbProject(folderKey);
    while (p) {
      names.unshift(p.name || t("fb.addto.untitled"));
      p = fbProject(fbParentOf(p));
    }
    return names.length ? names.join(" / ") : t("fb.root");
  }

  // 切片所在位置文本（搜索结果行）：首个所在文件夹路径 +「+N」（多文件夹）；
  // 无项目：临时查看 → 「临时查看」；否则「未归类」。
  function fbSlideLocationText(s) {
    var paths = [];
    allProjects.forEach(function (p) {
      var inP = (s.slide_id && (p.slide_ids || []).indexOf(s.slide_id) >= 0) ||
                (s.name && (p.slides || []).indexOf(s.name) >= 0);
      if (inP) paths.push(fbPathOf(p.pid));
    });
    if (!paths.length) {
      return s.temporary_view_expires_at ? t("fb.temp.folder") : t("fb.unfiled");
    }
    return paths[0] + (paths.length > 1 ? " +" + (paths.length - 1) : "");
  }

  // 搜索（§5.4）：在 /api/slides 已鉴权全量上本地匹配显示名/别名/原文件名
  // （含 legacy 名），大小写不敏感，不受当前文件夹限制。
  function fbSearchSlides(q) {
    var needle = String(q == null ? "" : q).trim().toLowerCase();
    if (!needle) return [];
    return allSlides.filter(function (s) {
      var hay = [s.display_name, s.alias, s.original_filename, s.name]
        .map(function (v) { return String(v == null ? "" : v).toLowerCase(); })
        .join("\n");
      return hay.indexOf(needle) >= 0;
    });
  }

  // 当前文件夹内容：子文件夹卡（真实项目）+ 根目录附加「临时查看」虚拟文件夹
  // + 切片列表
  function fbEntries(folderKey) {
    var folders = fbChildFolders(folderKey).map(function (p) {
      return {
        kind: "folder",
        key: p.pid,
        name: p.name || t("fb.addto.untitled"),
        count: fbSubtreeCount(p.pid),
      };
    });
    var slides;
    if (!folderKey) {
      if (fbTempSlides().length) {
        folders.push({
          kind: "folder", virtual: true, key: FB_TEMP_KEY,
          name: t("fb.temp.folder"), count: fbTempSlides().length,
        });
      }
      slides = fbUnfiledSlides();
    } else if (folderKey === FB_TEMP_KEY) {
      slides = fbTempSlides();
    } else {
      var p = fbProject(folderKey);
      slides = p ? fbFolderProjectSlides(p) : [];
    }
    return { folders: folders, slides: slides };
  }

  // 进入文件夹（翻页记忆：离开时存当前页，回来恢复；§5.3）。
  // folder-experience §2：导航结束即写位置记忆（本会话导航优先于磁盘记忆）。
  function fbGo(folderKey, pageHint, pinKey) {
    fbState.pages[fbState.folder] = fbState.page;
    fbState.folder = folderKey || null;
    fbState.pin = pinKey || null;
    fbState.page = (typeof pageHint === "number" && pageHint >= 0)
      ? pageHint : (fbState.pages[fbState.folder] || 0);
    renderFolderBrowser();
    fbNavTouched = true;
    fbMemorySave();
    // 焦点管理：进入（非返回）时落在新内容首个可聚焦元素；根目录回落返回钮
    var first = els.fbStack && els.fbStack.querySelector
      ? els.fbStack.querySelector(".fb-folder, .fb-hit") : null;
    var focusTarget = first || (fbState.folder ? null : els.fbUpBtn);
    if (focusTarget && typeof focusTarget.focus === "function") {
      try { focusTarget.focus(); } catch (e) { /* 忽略聚焦失败 */ }
    }
  }

  // 定位切片所在文件夹并翻到其所在叠（搜索结果点击 / 建后定位共用）。
  // 在与渲染完全相同的**合并有序条目**（文件夹前、切片后）里找索引，再用
  // 同一个装箱函数折算页码——修复只按切片序号算页（文件夹偏移被漏掉）。
  function fbLocateSlide(sinfo) {
    if (!sinfo) return;
    var ref = String(sinfo.slide_id || sinfo.name);
    var pid = null;
    for (var i = 0; i < allProjects.length; i++) {
      var p = allProjects[i];
      var inP = (sinfo.slide_id && (p.slide_ids || []).indexOf(sinfo.slide_id) >= 0) ||
                (sinfo.name && (p.slides || []).indexOf(sinfo.name) >= 0);
      if (inP) { pid = p.pid; break; }
    }
    var key = pid || (sinfo.temporary_view_expires_at ? FB_TEMP_KEY : null);
    var ordered = fbOrderedEntries(key);
    var idx = -1;
    ordered.forEach(function (it, j) {
      if (idx < 0 && it.kind === "slide" && it.sid === ref) idx = j;
    });
    if (idx < 0) { fbGo(key, 0); return; }
    var pages = fbPackPages(ordered, fbStackAvailH());
    var page = 0;
    for (; page < pages.length; page++) {
      if (idx < pages[page].length) break;
      idx -= pages[page].length;
    }
    if (page >= pages.length) page = pages.length - 1;
    fbGo(key, page, fbEntryKey({ kind: "slide", sid: ref }));
  }

  var FB_FOLDER_SVG = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>';

  function fbEntryKey(it) {
    return it.kind === "folder" ? "f:" + it.key : "s:" + it.sid;
  }

  // 页码锚定：定位到的切片（pin，翻页/换目录时清除）在任何重渲后都留在
  // 当前页；仅布局变化（relayout：可用高度变了、内容没变）时以本页首条
  // （anchor）为准重算页码——否则同一页码在新装箱下会指向别的切片。
  function fbAnchoredPage(pages, relayout) {
    var key = fbState.pin || (relayout ? fbState.anchor : null);
    if (!key) return null;
    for (var i = 0; i < pages.length; i++) {
      for (var j = 0; j < pages[i].length; j++) {
        if (fbEntryKey(pages[i][j]) === key) return i;
      }
    }
    return null;
  }

  // 渲染当前文件夹（整格重渲；页码在渲染前钳回有效范围）
  function renderFolderBrowser(relayout) {
    if (!els.fbStack) return;
    fbPulloutOnLayoutChange(); // 重渲后命中区全部重建：抽出层立即收回（§1.1）
    // 当前文件夹被删除后回到根目录（§5.3）；临时文件夹恒可用
    if (fbState.folder && fbState.folder !== FB_TEMP_KEY && !fbProject(fbState.folder)) {
      fbState.folder = null;
      fbState.page = 0;
    }
    var availH = fbStackAvailH();
    // 与搜索定位共用同一有序条目 + 装箱函数（§5.2）：页按本页条目真实高度
    // 装箱（文件夹卡 64+8、切片卡整卡/露出条带），≤8 张/页，下限 1。
    var ordered = fbOrderedEntries(fbState.folder);
    var pages = fbPackPages(ordered, availH);
    var totalItems = ordered.length;
    var totalPages = pages.length;
    var anchored = fbAnchoredPage(pages, relayout === true);
    if (anchored !== null) fbState.page = anchored;
    fbState.page = Math.max(0, Math.min(fbState.page, totalPages - 1));
    fbState.totalPages = totalPages;
    var pageEntries = pages[fbState.page] || [];
    if (relayout !== true && fbStackMeasured()) {
      // 侧栏收起（宽 0）时的装箱不代表真实布局：沿用原锚点，展开后据此回页
      fbState.anchor = pageEntries.length ? fbEntryKey(pageEntries[0]) : null;
    }

    els.fbStack.innerHTML = "";
    var top = 0;
    var activeRef = activeSlideRef();
    var focusIdx = 0;
    // 露出条带按**本页**的文件夹卡数与切片数自适应（短叠铺满、上限整卡高）
    var pageFolders = 0;
    pageEntries.forEach(function (e) { if (e.kind === "folder") pageFolders += 1; });
    var pageSlides = pageEntries.length - pageFolders;
    var gap = fbBandGap(availH, pageFolders, pageSlides);
    pageEntries.forEach(function (entry, j) {
      var node;
      if (entry.kind === "folder") {
        node = buildFbFolderCard(entry, top, focusIdx);
        top += FB_FOLDER_H + FB_FOLDER_GAP;
      } else {
        var isLast = j === pageEntries.length - 1;
        node = buildFbSlideCard(entry, top, focusIdx, activeRef, isLast, gap);
        top += isLast ? FB_CARD_H : gap;
      }
      focusIdx += 1;
      els.fbStack.appendChild(node);
    });
    // 高度不写死：.fb-stack 由 flex 撑满头部/翻页之间的剩余空间（页码恒在
    // 底部），条带高度按上面的自适应值铺开；翻页只换列表不动布局骨架。

    // 头部：位置 + 返回 + 文件夹 ⋯ + 计数
    var isRoot = !fbState.folder;
    var isTemp = fbState.folder === FB_TEMP_KEY;
    var curP = fbProject(fbState.folder);
    if (els.fbLocation) {
      var locText = isRoot ? t("fb.root") : (isTemp ? t("fb.temp.folder") : (curP ? curP.name || t("fb.addto.untitled") : t("fb.root")));
      els.fbLocation.textContent = locText;
      els.fbLocation.title = isRoot || isTemp ? locText : fbPathOf(fbState.folder);
    }
    if (els.fbUpBtn) els.fbUpBtn.hidden = isRoot;
    if (els.fbFolderBtn) els.fbFolderBtn.hidden = isRoot || isTemp;
    if (els.fbCount) els.fbCount.textContent = String(totalItems);
    if (els.fbPageInfo) {
      els.fbPageInfo.textContent = t("fb.page.info", { p: fbState.page + 1, m: totalPages });
    }
    if (els.fbPrevBtn) els.fbPrevBtn.disabled = fbState.page === 0;
    if (els.fbNextBtn) els.fbNextBtn.disabled = fbState.page >= totalPages - 1;
  }

  function buildFbFolderCard(entry, top, focusIdx) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "fb-folder";
    b.style.top = top + "px";
    b.style.zIndex = String(focusIdx + 1);
    b.dataset.pid = entry.key;
    b.setAttribute("aria-label", t("fb.open.folder.aria", { name: entry.name }));
    var icon = document.createElement("span");
    icon.className = "fb-folder-icon";
    icon.innerHTML = FB_FOLDER_SVG;
    b.appendChild(icon);
    var text = document.createElement("span");
    text.className = "fb-folder-text";
    var nameEl = document.createElement("span");
    nameEl.className = "fb-folder-name";
    nameEl.textContent = entry.name;
    if (entry.virtual) {
      var tempTag = document.createElement("span");
      tempTag.className = "fb-temp-tag";
      tempTag.textContent = t("tempview.badge");
      nameEl.appendChild(tempTag);
    }
    var countEl = document.createElement("small");
    countEl.className = "fb-folder-count";
    countEl.textContent = t("fb.folder.slides", { n: entry.count });
    text.appendChild(nameEl);
    text.appendChild(countEl);
    b.appendChild(text);
    b.addEventListener("click", function () { fbGo(entry.key); });
    b.addEventListener("keydown", function (e) { fbStackKeydown(e, b); });
    return b;
  }

  function buildFbSlideCard(entry, top, focusIdx, activeRef, isLast, gap) {
    var s = entry.s;
    var sid = entry.sid;
    var sname = entry.name;
    var failed = !!s.__missing || (!s.slide_id && !s.name);
    var dispName = s.__missing ? sname : slideDisplayName(s);
    var active = !!activeRef && activeRef === String(sid);

    // 命中区：卡片原位置的窄条（含 ⋯ 钮），命中区稳定不随预览位移（§5.2）
    var hit = document.createElement("button");
    hit.type = "button";
    hit.className = "fb-hit" + (isLast ? " fb-hit-last" : "") + (active ? " active" : "");
    hit.style.top = top + "px";
    // 命中区：非末张=露出条带；末张=整卡高（无遮挡，可点整卡）
    hit.style.height = (isLast ? FB_CARD_H : Math.max(gap || FB_GAP, FB_MIN_GAP)) + "px";
    hit.style.zIndex = String(focusIdx + 1);
    hit.dataset.slideId = sid;
    hit.dataset.name = sname;
    hit.setAttribute("aria-pressed", active ? "true" : "false");
    hit.setAttribute("aria-label", t("fb.open.slide.aria", { name: dispName || sname }));

    // 悬停预览卡（完整缩略图 + 名称）：浮层不改变布局；pointer-events 只在
    // 滑出态开启，避免遮住下方卡片命中区造成抖动
    var card = document.createElement("span");
    card.className = "fb-card";
    var label = document.createElement("span");
    label.className = "fb-card-label";
    var nameEl = document.createElement("span");
    nameEl.className = "fb-card-name";
    nameEl.textContent = truncateMiddle(dispName || sname, 16) + (failed ? t("slide.read.fail.short") : "");
    nameEl.title = (dispName || sname) + (failed ? " " + t("slide.read.fail") : "");
    label.appendChild(nameEl);
    // 标注徽章（次行信息在滑出时完整可见；作者口径见 annoBadgeText）
    var badgeText = failed ? null : annoBadgeText(sid);
    if (badgeText) {
      var badge = document.createElement("span");
      badge.className = "fb-card-badge";
      badge.textContent = badgeText;
      badge.title = t("slide.anno.badge.title");
      label.appendChild(badge);
    }
    if (s.temporary_view_expires_at) {
      var tempTag = document.createElement("span");
      tempTag.className = "fb-card-temp";
      tempTag.textContent = t("tempview.badge");
      label.appendChild(tempTag);
    }
    var dot = document.createElement("i");
    dot.className = "fb-card-dot";
    dot.textContent = active ? "●" : "";
    label.appendChild(dot);
    card.appendChild(label);
    // 缩略图：只为当前叠创建（渲染即当前叠）；失败显示占位（CSS 类）
    if (!failed) {
      var img = document.createElement("img");
      img.className = "fb-card-img";
      img.alt = "";
      img.setAttribute("loading", "lazy");
      img.src = slideThumbnailUrl(sid);
      img.addEventListener("error", function () {
        card.classList.add("fb-thumb-error");
        img.removeAttribute("src");
      });
      card.appendChild(img);
    } else {
      card.classList.add("fb-thumb-error");
    }
    hit.appendChild(card);

    // 卡片「⋯」菜单：保留全部单片操作（重命名/备注、加入文件夹、从此文件夹
    // 移出、分享、Demo 目录、删除）
    var menuBtn = document.createElement("button");
    menuBtn.type = "button";
    menuBtn.className = "fb-card-menu";
    menuBtn.textContent = "⋯";
    menuBtn.setAttribute("aria-haspopup", "menu");
    menuBtn.setAttribute("aria-label", t("fb.folder.menu.aria"));
    menuBtn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      fbPulloutHide();
      openFbSlideMenu(menuBtn, s, sid, sname);
    });
    hit.appendChild(menuBtn);

    // The first hover opens the fan; moving within it has no per-card delay.
    hit.addEventListener("pointerenter", function (e) {
      if (e && (e.pointerType === "touch" || e.buttons)) return;
      if (fbFanDismissedAt && e && Math.abs(e.clientX - fbFanDismissedAt.x) < 2 && Math.abs(e.clientY - fbFanDismissedAt.y) < 2) return;
      if (fbFan) { fbFanMove(e); return; }
      if (fbPulloutShowTimer) clearTimeout(fbPulloutShowTimer);
      fbPulloutShowTimer = setTimeout(function () {
        fbPulloutShowTimer = null;
        fbPulloutShow(hit);
        fbFanMove(e);
      }, 80);
    });
    hit.addEventListener("pointerleave", function () {
      if (fbPulloutShowTimer) { clearTimeout(fbPulloutShowTimer); fbPulloutShowTimer = null; }
      fbPulloutScheduleHide();
    });
    hit.addEventListener("focus", function () {
      if (hit.matches && hit.matches(":focus-visible")) fbPulloutShow(hit);
    });
    hit.addEventListener("blur", function () {
      fbPulloutScheduleHide();
    });
    hit.addEventListener("click", function () {
      // Snapshot the floating card before removing the fan portals.
      var source = captureSlideCard(hit);
      fbPulloutHide();
      openSlide(sid, source);
    });
    hit.addEventListener("keydown", function (e) { fbStackKeydown(e, hit); });
    return hit;
  }

  // 堆叠键盘导航：↑/↓ 在卡片（含文件夹卡）间移动，Enter 原生触发打开
  function fbStackKeydown(e, node) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    var kids = els.fbStack ? els.fbStack.querySelectorAll(".fb-folder, .fb-hit") : [];
    var idx = -1;
    for (var i = 0; i < kids.length; i++) { if (kids[i] === node) { idx = i; break; } }
    var next = kids[idx + (e.key === "ArrowDown" ? 1 : -1)];
    if (next && typeof next.focus === "function") { try { next.focus(); } catch (err) {} }
  }

  // Continuous, distance-based fan (Dock-style magnification). Geometry comes
  // from the stationary hit bands, never the animated cards. One portal per
  // visible slide is reused for the whole gesture; at most eight on a page.
  var fbPulloutEl = null;
  var fbPulloutHit = null;
  var fbPulloutHideTimer = null;
  var fbPulloutShowTimer = null;
  var fbFan = null;
  var fbFanDismissedAt = null;

  function fbPulloutKeep() {
    if (fbPulloutHideTimer) { clearTimeout(fbPulloutHideTimer); fbPulloutHideTimer = null; }
  }

  function fbPulloutHide() {
    if (fbPulloutShowTimer) { clearTimeout(fbPulloutShowTimer); fbPulloutShowTimer = null; }
    fbPulloutKeep();
    if (fbFan) {
      fbFan.items.forEach(function (item) {
        item.hit.classList.remove("extracted", "fb-preview-source");
        if (item.pop.parentNode) item.pop.parentNode.removeChild(item.pop);
      });
    }
    fbFan = null;
    fbPulloutEl = null;
    fbPulloutHit = null;
    document.body.classList.remove("fb-pullout-on");
  }

  function fbPulloutScheduleHide() {
    fbPulloutKeep();
    fbPulloutHideTimer = setTimeout(fbPulloutHide, 90);
  }

  function fbFanSelect(index) {
    if (!fbFan) return;
    fbFan.items.forEach(function (item, i) {
      var active = i === index;
      item.hit.classList.toggle("extracted", active);
      item.pop.classList.toggle("fb-pullout", active);
      if (item.menu) item.menu.hidden = !active;
      if (active) { fbPulloutHit = item.hit; fbPulloutEl = item.pop; }
    });
  }

  function fbFanPaint(fan) {
    if (fbFan !== fan) return;
    fan.pending = false;
    var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    fan.items.forEach(function (item, i) {
      var distance = i - fan.position;
      // File-pocket metaphor: the card under the pointer slides sideways out of
      // the stack at its own size; neighbours follow a little. No lift, scale
      // or tilt, so the stack reads as paper in a folder, not floating tiles.
      // The gaussian keeps motion continuous across band boundaries.
      var dx = Math.min(FB_PULL_PX * Math.exp(-Math.pow(distance / .75, 2)),
        Math.max(0, fan.right - item.rect.right));
      // Only clamp vertically so a card near the bottom stays on screen.
      var dy = Math.max(fan.top, Math.min(item.rect.top, fan.bottom - FB_CARD_H)) - item.rect.top;
      item.pop.style.left = (reduced ? item.rect.left + dx : item.rect.left).toFixed(2) + "px";
      item.pop.style.top = (reduced ? item.rect.top + dy : item.rect.top).toFixed(2) + "px";
      item.pop.style.width = item.rect.width.toFixed(2) + "px";
      item.pop.style.height = FB_CARD_H + "px";
      item.pop.style.transform = reduced ? "none" :
        "translate3d(" + dx.toFixed(2) + "px," + dy.toFixed(2) + "px,0)";
      item.pop.style.zIndex = String(item.hit === fbPulloutHit ? 515 : 505 + i);
    });
  }

  function fbFanPosition(position, index) {
    if (!fbFan) return;
    fbPulloutKeep();
    fbFan.position = position;
    fbFanSelect(index);
    if (fbFan.pending) return;
    fbFan.pending = true;
    var fan = fbFan;
    (window.requestAnimationFrame || function (cb) { setTimeout(cb, 16); })(function () { fbFanPaint(fan); });
  }

  function fbFanMove(e) {
    if (!fbFan || !e || e.pointerType === "touch" || e.buttons ||
        typeof e.clientX !== "number" || typeof e.clientY !== "number") return;
    // An expanded card can cover several rows. In the sidebar, use pointer Y
    // against original bands regardless of which portal received this event.
    var items = fbFan.items;
    var first = items[0].rect, last = items[items.length - 1].rect;
    if (e.clientX < first.left || e.clientX > first.right || e.clientY < first.top || e.clientY >= last.bottom) return;
    var index = items.length - 1;
    for (var i = 0; i < items.length; i++) {
      if (e.clientY < items[i].rect.bottom) { index = i; break; }
    }
    var position = index;
    var center = items[index].center;
    var adjacent = e.clientY < center ? index - 1 : index + 1;
    if (items[adjacent]) {
      position += (e.clientY - center) / Math.abs(items[adjacent].center - center);
    }
    fbFanPosition(Math.max(0, Math.min(items.length - 1, position)), index);
  }

  function fbFanActivate(e, menu) {
    if (!fbFan || !fbPulloutHit) return;
    // Resolve from the current pointer, not the node left under it by animation.
    fbFanMove(e);
    var hit = fbPulloutHit;
    fbFanDismissedAt = e && typeof e.clientX === "number" ? { x: e.clientX, y: e.clientY } : null;
    var sourceMenu = hit.querySelector(".fb-card-menu");
    if (menu && sourceMenu) { fbPulloutHide(); sourceMenu.click(); }
    else hit.click();
  }

  function fbPulloutShow(hit) {
    if (!hit || !hit.querySelector(".fb-card")) return;
    fbPulloutKeep();
    if (fbFan) {
      for (var n = 0; n < fbFan.items.length; n++) {
        if (fbFan.items[n].hit === hit) { fbFanPosition(n, n); return; }
      }
      fbPulloutHide();
    }
    var nodes = Array.prototype.filter.call(hit.parentNode ? hit.parentNode.children : [hit], function (node) {
      return node.classList.contains("fb-hit");
    });
    if (nodes.indexOf(hit) < 0) nodes = [hit];
    var stackRect = els.fbStack && els.fbStack.getBoundingClientRect ? els.fbStack.getBoundingClientRect() : null;
    var fan = {
      items: [], position: 0, pending: false,
      top: Math.max(16, stackRect && stackRect.height ? nodes[0].getBoundingClientRect().top - 8 : 16),
      bottom: Math.min(window.innerHeight - 16, stackRect && stackRect.height ? stackRect.bottom : window.innerHeight - 16),
      right: window.innerWidth - 16,
    };
    nodes.forEach(function (node, i) {
      var rect = node.getBoundingClientRect();
      var pop = document.createElement("div");
      pop.className = "fb-fan-card";
      pop.dataset.slideId = node.dataset.slideId || "";
      pop.setAttribute("aria-hidden", "true");
      var clone = node.querySelector(".fb-card").cloneNode(true);
      clone.className = "fb-card fb-pullout-card";
      pop.appendChild(clone);
      var sourceMenu = node.querySelector(".fb-card-menu");
      var menu = null;
      if (sourceMenu) {
        menu = sourceMenu.cloneNode(true);
        menu.tabIndex = -1;
        menu.hidden = true;
        menu.addEventListener("click", function (e) { e.stopPropagation(); fbFanActivate(e, true); });
        pop.appendChild(menu);
      }
      var next = nodes[i + 1];
      var band = next ? next.getBoundingClientRect().top - rect.top
        : (i > 0 ? rect.top - fan.items[i - 1].rect.top : rect.height);
      fan.items.push({ hit: node, pop: pop, menu: menu, rect: rect, center: rect.top + Math.min(rect.height, band) / 2 });
      pop.addEventListener("pointerenter", fbPulloutKeep);
      pop.addEventListener("pointerleave", fbPulloutScheduleHide);
      pop.addEventListener("click", function (e) { fbFanActivate(e, false); });
      document.body.appendChild(pop);
      node.classList.add("fb-preview-source");
    });
    fbFan = fan;
    document.body.classList.add("fb-pullout-on");
    var index = nodes.indexOf(hit);
    fan.position = index;
    fbFanSelect(index);
    fbFanPaint(fan);
  }

  function fbPulloutOnLayoutChange() { fbPulloutHide(); }

  // ---------- 通用小菜单（＋ / 文件夹 ⋯ / 卡片 ⋯ / 加入文件夹 / 移动到） ----------
  function closeFbMenuEl(menu) {
    if (menu && menu.parentNode) menu.parentNode.removeChild(menu);
    if (menu && menu.setAttribute) menu.setAttribute("hidden", "");
  }
  function openFbMenuEl(menu, anchor) {
    // 挂 body（避免侧栏收起/overflow 裁剪）；固定定位在锚点下方，越界钳回
    closeFbMenuEl(menu);
    if (menu.removeAttribute) menu.removeAttribute("hidden");
    document.body.appendChild(menu);
    var r = anchor.getBoundingClientRect ? anchor.getBoundingClientRect()
      : { left: 8, right: 40, bottom: 40, top: 20 };
    var vw = window.innerWidth || 1024;
    var vh = window.innerHeight || 768;
    var mw = menu.offsetWidth || 160;
    var mh = menu.offsetHeight || 120;
    var left = Math.min(r.left, vw - mw - 8);
    if (left < 8) left = 8;
    var top2 = r.bottom + 4;
    if (top2 + mh > vh - 8 && r.top - mh - 4 >= 8) top2 = r.top - mh - 4;
    menu.style.left = Math.round(left) + "px";
    menu.style.top = Math.round(top2) + "px";
    menu.classList.add("open");
    function onDocClick(ev) {
      var tgt = ev.target;
      // Card menu anchors have no id; containment also handles their SVG children.
      if (tgt && (menu.contains(tgt) || anchor.contains(tgt))) return;
      closeFbMenu();
    }
    function onKey(ev) { if (ev.key === "Escape") closeFbMenu(); }
    var closeFbMenu = function () {
      closeFbMenuEl(menu);
      document.removeEventListener("click", onDocClick, true);
      document.removeEventListener("keydown", onKey, true);
      if (anchor.setAttribute) anchor.setAttribute("aria-expanded", "false");
    };
    setTimeout(function () {
      document.addEventListener("click", onDocClick, true);
      document.addEventListener("keydown", onKey, true);
    }, 0);
    menu.__close = closeFbMenu;
    if (anchor.setAttribute) anchor.setAttribute("aria-expanded", "true");
    return closeFbMenu;
  }
  function menuItemEl(label, onClick, danger) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "fb-menu-item" + (danger ? " danger" : "");
    b.setAttribute("role", "menuitem");
    b.textContent = label;
    b.addEventListener("click", function (e) {
      e.stopPropagation();
      var menu = b.parentNode;
      if (menu && typeof menu.__close === "function") menu.__close();
      else closeFbMenuEl(menu);
      onClick();
    });
    return b;
  }

  // 卡片「⋯」菜单：单片操作一项不少（§5.2）
  function openFbSlideMenu(anchor, s, sid, sname) {
    var menu = document.createElement("div");
    menu.id = "fb-slide-menu";
    menu.className = "fb-menu fb-slide-menu";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", sname || sid);
    menu.appendChild(menuItemEl(t("fb.menu.rename"), function () { promptSlideMeta(sname, s, sid); }));
    menu.appendChild(menuItemEl(t("fb.menu.addto"), function () { openFbAddToFolder(anchor, s, sid); }));
    if (fbState.folder && fbState.folder !== FB_TEMP_KEY) {
      menu.appendChild(menuItemEl(t("fb.menu.remove"), function () { removeSlideFromFolder(s, sid, fbState.folder); }));
    }
    menu.appendChild(menuItemEl(t("fb.menu.share"), function () { doCreateShare([sid]); }));
    // Demo 目录按钮（仅 owner；与旧行内 ▣/▢ 同一能力）
    if (s && s.name) {
      var inCatalog = !!demoCatalogNames[s.name];
      menu.appendChild(menuItemEl(t(inCatalog ? "demo.catalog.remove" : "demo.catalog.add"),
        function () { toggleDemoCatalog(s.name); }));
    }
    menu.appendChild(menuItemEl(t("fb.menu.delete"),
      function () { deleteSlide(slideDisplayName(s) || sname, (s && s.slide_id) || sid, s ? s.name : sname); }, true));
    openFbMenuEl(menu, anchor);
  }

  // 「加入文件夹…」：把这张切片 POST 到所选项目（与切片选择器确认同端点）
  function openFbAddToFolder(anchor, s, sid) {
    var menu = document.createElement("div");
    menu.id = "fb-addto-menu";
    menu.className = "fb-menu fb-slide-menu";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", t("fb.addto.title"));
    if (!allProjects.length) {
      var empty = document.createElement("div");
      empty.className = "fb-menu-note";
      empty.textContent = t("proj.empty");
      menu.appendChild(empty);
    }
    allProjects.forEach(function (p) {
      menu.appendChild(menuItemEl(truncateMiddle(p.name || t("fb.addto.untitled"), 18), function () {
        apiFetch("/api/project/" + encodeURIComponent(p.pid) + "/slides", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(slideRefsPayload([sid])),
        })
          .then(function (r) {
            if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("fb.addto.fail", { e: r.status })); });
            return r.json();
          })
          .then(function () { toast(t("fb.addto.done", { name: p.name || "" }), "success"); reloadProjectsAndUnfiled(); })
          .catch(function (e) { toast(t("fb.addto.fail", { e: e.message }), "error"); });
      }));
    });
    openFbMenuEl(menu, anchor);
  }

  // 「从此文件夹移出」：只删关联；失去全部关联的切片回到根目录「未归类」（§5.3）
  function removeSlideFromFolder(s, sid, pid) {
    var byId = !!(s && s.slide_id) && slideIdApiOn();
    var url = byId
      ? "/api/project/" + encodeURIComponent(pid) + "/slides/" + encodeURIComponent(s.slide_id)
      : "/api/project/" + encodeURIComponent(pid) + "/slide/" + encodeURIComponent((s && s.name) || sid);
    apiFetch(url, { method: "DELETE" })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("fb.remove.fail", { e: r.status })); });
        return r.json();
      })
      .then(function () { toast(t("fb.remove.done"), "success"); reloadProjectsAndUnfiled(); })
      .catch(function (e) { toast(t("fb.remove.fail", { e: e.message }), "error"); });
  }

  // 「移动到…」：根目录 + 其余文件夹（排除自身与自身子树；环检测由服务端
  // 事务内做，前端只做同口径预检与可读报错）。本分支后端未合入
  // parent_project_id 时 PATCH 会忽略该字段——行为=移动暂不生效（可验证期
  // 在合并后）。
  function openFbMoveDialog(anchor) {
    var cur = fbProject(fbState.folder);
    if (!cur) return;
    var menu = document.createElement("div");
    menu.id = "fb-move-menu";
    menu.className = "fb-menu fb-slide-menu";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", t("fb.move.title"));
    function moveTo(targetPid, targetName) {
      apiFetch("/api/project/" + encodeURIComponent(cur.pid), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parent_project_id: targetPid }),
      })
        .then(function (r) {
          if (!r.ok) {
            return r.json().then(function (j) {
              throw new Error(j.error || t("fb.move.fail", { e: r.status }));
            });
          }
          return r.json();
        })
        .then(function () {
          toast(t("fb.move.done", { name: targetName }), "success");
          fbState.pages[fbState.folder] = 0;
          reloadProjectsAndUnfiled();
        })
        .catch(function (e) { toast(t("fb.move.fail", { e: e.message }), "error"); });
    }
    menu.appendChild(menuItemEl(t("fb.move.root"), function () { moveTo(null, t("fb.root")); }));
    allProjects.forEach(function (p) {
      if (p.pid === cur.pid) return;
      if (fbInSubtree(p.pid, cur.pid)) return; // 不能移到自己或自己的子孙
      menu.appendChild(menuItemEl(truncateMiddle(fbPathOf(p.pid), 24), function () {
        moveTo(p.pid, p.name || "");
      }));
    });
    openFbMenuEl(menu, anchor);
  }

  // ---------- 浏览器头部/翻页绑定（bindEvents 调用；Demo 壳无此 DOM 时全部跳过） ----------
  var fbResizePending = false;
  function initFbBrowser() {
    if (!els.fbStack) return;
    if (els.fbUpBtn) {
      els.fbUpBtn.addEventListener("click", function () {
        // 返回上级：恢复该目录上次所在叠（fbGo 内读 pages 记忆）
        var key = fbState.folder === FB_TEMP_KEY ? null : fbParentOf(fbProject(fbState.folder) || {});
        fbGo(key || null);
      });
    }
    if (els.fbPrevBtn) {
      els.fbPrevBtn.addEventListener("click", function () {
        if (fbState.page > 0) { fbState.page -= 1; fbState.pin = null; renderFolderBrowser(); fbMemorySave(); }
      });
    }
    if (els.fbNextBtn) {
      els.fbNextBtn.addEventListener("click", function () {
        if (fbState.page < fbState.totalPages - 1) {
          fbState.page += 1; fbState.pin = null; renderFolderBrowser(); fbMemorySave();
        }
      });
    }
    // ＋ 菜单（导入切片 / 新建文件夹）
    if (els.fbPlusBtn && els.fbPlusMenu) {
      els.fbPlusMenu.addEventListener("click", function (e) {
        // 菜单项点击后收起（导入抽屉/新建对话框各自打开）
        if (e.target && e.target.closest && e.target.closest("button")) {
          if (typeof els.fbPlusMenu.__close === "function") els.fbPlusMenu.__close();
        }
      });
      els.fbPlusBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        if (els.fbPlusMenu.parentNode === document.body) {
          if (els.fbPlusMenu.__close) els.fbPlusMenu.__close();
          return;
        }
        openFbMenuEl(els.fbPlusMenu, els.fbPlusBtn);
      });
    }
    // 当前文件夹 ⋯ 菜单（重命名 / 移动到… / 分享此文件夹 / 删除文件夹）
    if (els.fbFolderBtn && els.fbFolderMenu) {
      els.fbFolderMenu.addEventListener("click", function (e) {
        if (e.target && e.target.closest && e.target.closest("button")) {
          if (typeof els.fbFolderMenu.__close === "function") els.fbFolderMenu.__close();
        }
      });
      els.fbFolderBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        if (els.fbFolderMenu.parentNode === document.body) {
          if (els.fbFolderMenu.__close) els.fbFolderMenu.__close();
          return;
        }
        openFbMenuEl(els.fbFolderMenu, els.fbFolderBtn);
      });
    }
    if (els.fbRename) els.fbRename.addEventListener("click", function () {
      var p = fbProject(fbState.folder);
      if (p) editProject(p);
    });
    if (els.fbMove) els.fbMove.addEventListener("click", function () { openFbMoveDialog(els.fbFolderBtn); });
    if (els.fbShareFolder) els.fbShareFolder.addEventListener("click", function () {
      var p = fbProject(fbState.folder);
      if (p) shareProject(p);
    });
    if (els.fbDelete) els.fbDelete.addEventListener("click", function () {
      var p = fbProject(fbState.folder);
      if (!p) return;
      // 确认框说明：子文件夹回到根、切片不删（§5.3）——仅此一层确认
      //（DEF-1：不再叠加 deleteProject 的旧版「删除项目」确认）
      if (!confirm(t("fb.del.confirm", { name: p.name || "" }))) return;
      deleteProject(p, { skipConfirm: true });
    });
    // 可用高度变化 → 叠大小重算（rAF 节流，翻页不变）。除窗口 resize 外，
    // 栈自身高度也会变（侧栏收起/展开过渡中头部换行会暂时压缩 .fb-stack）：
    // 只听 window resize 会把过渡期的瞬时高度装箱结果（每页 1 张）一直留存。
    // 渲染只重建栈内绝对定位子节点、不改变栈自身盒尺寸，观察不会回环。
    function scheduleFbRelayout() {
      if (fbResizePending) return;
      fbResizePending = true;
      (window.requestAnimationFrame || function (cb) { setTimeout(cb, 16); })(function () {
        fbResizePending = false;
        renderFolderBrowser(true);
      });
    }
    window.addEventListener("resize", scheduleFbRelayout);
    if (typeof ResizeObserver === "function") {
      new ResizeObserver(scheduleFbRelayout).observe(els.fbStack);
    }
    // 首帧（loadAll 之前）先渲染空态，避免空白侧栏
    renderFolderBrowser();
  }

  // 行内别名/备注编辑（卡片「⋯ → 重命名 / 备注」）：两次 prompt 收集
  // display_name/note，ID 通道 PATCH /api/slides/<slide_id>；旧后端回落
  // POST /api/slide/<name>/meta（alias 入参服务端映射 display_name）。
  // 叠卡空间放不下行内表单，改用 prompt（与项目重命名同一交互档）。
  function promptSlideMeta(sname, sinfo, sid) {
    var alias0 = (sinfo && (sinfo.display_name || sinfo.alias)) || "";
    var note0 = (sinfo && sinfo.note) || "";
    var alias = window.prompt(t("edit.alias.ph"), alias0);
    if (alias === null) return;
    var note = window.prompt(t("edit.note.ph"), note0);
    if (note === null) return;
    var req;
    if (slideIdApiOn() && sid && sinfo && sinfo.slide_id) {
      req = apiFetch("/api/slides/" + encodeURIComponent(sinfo.slide_id), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: alias, note: note }),
      });
    } else {
      req = apiFetch("/api/slide/" + encodeURIComponent(sname) + "/meta", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ alias: alias, note: note }),
      });
    }
    req
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("save.fail")); });
        return r.json();
      })
      .then(function () {
        toast(t("common.updated"), "success");
        reloadProjectsAndUnfiled();
      })
      .catch(function (e) { toast(t("save.fail2", { e: e.message }), "error"); });
  }

  // ---------- Demo 目录（owner allowlist，PT-4 docs §5.1） ----------
  var demoCatalogNames = {};
  var demoCatalogLoaded = false;

  function loadDemoCatalog() {
    apiFetch("/api/admin/demo-catalog").then(function (r) {
      if (!r.ok) return null; // json/dual 503 / 非 owner 403：按钮维持隐藏
      return r.json();
    }).then(function (data) {
      if (!data) return;
      demoCatalogNames = {};
      (data.slides || []).forEach(function (s) {
        if (s && s.name) demoCatalogNames[s.name] = true;
      });
      demoCatalogLoaded = true;
      document.querySelectorAll(".slide-demo").forEach(function (btn) {
        updateDemoBtn(btn, btn.dataset.name);
      });
    }).catch(function () { /* 目录状态读失败：按钮保持隐藏（fail-closed） */ });
  }

  function updateDemoBtn(btn, name) {
    if (!btn) return;
    if (!demoCatalogLoaded) { btn.style.display = "none"; return; }
    var inCatalog = !!demoCatalogNames[name];
    btn.style.display = "";
    btn.textContent = inCatalog ? "▣" : "▢";
    btn.title = inCatalog ? t("demo.catalog.remove") : t("demo.catalog.add");
    btn.setAttribute("aria-pressed", inCatalog ? "true" : "false");
  }

  function toggleDemoCatalog(name) {
    var inCatalog = !!demoCatalogNames[name];
    var confirmKey = inCatalog ? "demo.catalog.remove.confirm" : "demo.catalog.add.confirm";
    if (!confirm(t(confirmKey, { name: name }))) return;
    var req = inCatalog
      ? apiFetch("/api/admin/demo-catalog?slide=" + encodeURIComponent(name), { method: "DELETE" })
      : apiFetch("/api/admin/demo-catalog", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ slide: name }),
        });
    req.then(function (r) {
      return r.json().then(function (b) { return { ok: r.ok, body: b || {} }; },
                            function () { return { ok: r.ok, body: {} }; });
    }).then(function (res) {
      if (!res.ok) {
        toast(t("demo.catalog.fail", { e: res.body.error || "HTTP " }), "error");
        return;
      }
      toast(t(inCatalog ? "demo.catalog.done.remove" : "demo.catalog.done.add",
              { name: name }), "success");
      loadDemoCatalog(); // 重新拉权威状态（PUT/DELETE 响应不含展示名集合）
    }).catch(function (e) {
      toast(t("demo.catalog.fail", { e: (e && e.message) || e }), "error");
    });
  }

  // 列表查找（P2 合同 §5.1：按 id 定位；旧后端/名称快照按名——两个键空间
  // 不相交：slide_id 是 sld_ 前缀服务端随机串，文件名带扩展名）
  function findSlideInfo(ref) {
    if (ref == null) return null;
    for (var i = 0; i < allSlides.length; i++) {
      if (allSlides[i].slide_id === ref || allSlides[i].name === ref) {
        return allSlides[i];
      }
    }
    return null;
  }

  function findSlideInfoById(slideId) {
    if (!slideId) return null;
    for (var i = 0; i < allSlides.length; i++) {
      if (allSlides[i].slide_id === slideId) return allSlides[i];
    }
    return null;
  }

  // 项目行引用（与项目内顺序逐行对齐）：新后端 slide_refs；旧后端只有 slides(名)
  function projectSlideRefs(p) {
    if (p && Array.isArray(p.slide_refs)) {
      return p.slide_refs.filter(function (r) { return r && (r.slide_id || r.slide); });
    }
    return ((p && p.slides) || []).map(function (n) { return { slide: n, slide_id: null }; });
  }

  // ---------- 新建项目（W3：独立对话框；修 R3 草稿残留 / R4 重复提交） ----------
  // R3：旧实现把「显式空数组」回退到 pendingNewProjectSlides（未归类勾选的
  // 隐式全局），取消含选中后普通新建仍夹带旧选择。现在 empty / selection
  // 两种模式显式分离：selection 打开时**复制**当前勾选快照进草稿，取消 /
  // Esc / 重新以 empty 打开一律清空草稿；空数组必须表示空项目，绝无 fallback。
  // R4：inFlight 提交锁由确认按钮与 Enter 共用；Idempotency-Key 按草稿内容
  // 指纹生成，同一份草稿（含失败后重试）复用，编辑后换新键。
  var projectDialog = {
    open: false,
    mode: null,            // null | "empty" | "selection"
    slides: [],            // 草稿切片（快照副本，可逐项移除）
    inFlight: false,       // R4 提交锁
    idemKey: null,         // 当前草稿的 Idempotency-Key（未发送/已失败时保留）
    idemFingerprint: "",   // 生成 idemKey 时的载荷指纹
    lastFocusEl: null,     // 打开者（Esc/关闭后归还焦点）
    parentPid: null,       // 新建文件夹的父文件夹（当前所在文件夹；null=根）
  };

  function uuid() {
    try {
      if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
      }
    } catch (e) { /* 回退 */ }
    // RFC4122 v4 形态的伪随机回退（crypto.getRandomValues 缺失时的兜底）
    var s = "";
    for (var i = 0; i < 36; i++) {
      if (i === 8 || i === 13 || i === 18 || i === 23) { s += "-"; continue; }
      s += Math.floor(Math.random() * 16).toString(16);
    }
    return s;
  }

  function projectDialogFingerprint(name, note, slides, parentPid) {
    return JSON.stringify([name, note, slides.slice().sort(), parentPid || null]);
  }

  function pcdShowError(msg) {
    if (!els.pcdError) return;
    els.pcdError.textContent = msg || "";
    els.pcdError.hidden = !msg;
  }

  function renderProjectDialogSlides() {
    if (!els.pcdSlidesList) return;
    els.pcdSlidesList.innerHTML = "";
    var n = projectDialog.slides.length;
    if (els.pcdSlidesSummary) {
      els.pcdSlidesSummary.textContent = n
        ? t("pcd.slides.contains", { n: n })
        : t("pcd.slides.empty.hint");
    }
    if (!n) return;
    projectDialog.slides.forEach(function (ref) {
      // P2：草稿键是行操作键（slide_id）；chip 主显示 display_name/名，
      // title 辅助原始名/ID
      var sinfo = findSlideInfo(ref);
      var disp = sinfo ? (slideDisplayName(sinfo) || sinfo.name) : ref;
      var chip = document.createElement("span");
      chip.className = "pcd-slide-chip";
      var label = document.createElement("span");
      label.className = "pcd-slide-chip-name";
      label.textContent = truncateMiddle(disp, 28);
      label.title = (sinfo && sinfo.name) || ref;
      chip.appendChild(label);
      var rm = document.createElement("button");
      rm.type = "button";
      rm.className = "pcd-slide-chip-remove";
      rm.textContent = "×";
      rm.setAttribute("aria-label", t("pcd.slides.remove", { name: truncateMiddle(disp, 20) }));
      rm.addEventListener("click", function () {
        if (projectDialog.inFlight) return;  // 提交中不允许改草稿
        projectDialog.slides = projectDialog.slides.filter(function (s) { return s !== ref; });
        renderProjectDialogSlides();
      });
      chip.appendChild(rm);
      els.pcdSlidesList.appendChild(chip);
    });
  }

  // 手机端：侧栏抽屉与导入抽屉/对话框同为全屏浮层——打开后者前先收起侧栏
  // 抽屉（移动端 #sidebar 抽屉 z-index 300，若不收起会盖住遮罩劫持点击）。
  // 触发按钮多半在侧栏里（导入切片/新建项目都在侧栏顶部），随抽屉收起后
  // 不可聚焦，焦点归还目标改指向 ☰（closeDrawer 的 a11y 路径通常已先还给它）。
  function closeSidebarDrawerUnderOverlay(triggerEl) {
    if (!isMobileWidth() || !sidebarCtrl || !sidebarCtrl.isDrawerOpen ||
        !sidebarCtrl.isDrawerOpen()) {
      return triggerEl;
    }
    sidebarCtrl.closeDrawer();
    if (triggerEl && els.sidebar && els.sidebar.contains && els.sidebar.contains(triggerEl)) {
      return els.menuBtn || triggerEl;
    }
    return triggerEl;
  }

  // 打开对话框。mode="empty"：永远 slides=[]（普通新建）；
  // mode="selection"：复制 slidesSnapshot 快照（「新建项目(含选中)」）。
  function openProjectDialog(mode, slidesSnapshot, triggerEl) {
    if (!els.projectCreateMask) return;
    triggerEl = closeSidebarDrawerUnderOverlay(triggerEl);
    projectDialog.open = true;
    projectDialog.mode = mode === "selection" ? "selection" : "empty";
    // 改版 2026-10-08：对话框文案/提交目标 =「文件夹」；父文件夹 = 浏览器
    // 当前所在文件夹（根目录为 null）。后端未合入 parent_project_id 时该
    // 字段被忽略（行为=仍在根创建，合并后生效）。
    projectDialog.parentPid = (fbState.folder && fbState.folder !== FB_TEMP_KEY)
      ? fbState.folder : null;
    projectDialog.slides = projectDialog.mode === "selection" && Array.isArray(slidesSnapshot)
      ? slidesSnapshot.slice() : [];
    projectDialog.inFlight = false;
    // R3：每次打开都是新草稿——旧键/旧指纹/旧输入一并清空
    projectDialog.idemKey = null;
    projectDialog.idemFingerprint = "";
    projectDialog.lastFocusEl = triggerEl || null;
    if (els.pcdName) els.pcdName.value = "";
    if (els.pcdNote) els.pcdNote.value = "";
    pcdShowError("");
    renderProjectDialogSlides();
    if (els.pcdTitle) els.pcdTitle.textContent = t("pcd.title.folder");
    if (els.pcdConfirm) {
      els.pcdConfirm.disabled = false;
      els.pcdConfirm.textContent = t("pcd.confirm.folder");
    }
    els.projectCreateMask.hidden = false;
    if (els.pcdName && typeof els.pcdName.focus === "function") {
      try { els.pcdName.focus(); } catch (e) { /* 忽略聚焦失败 */ }
    }
  }

  // 关闭并**清空草稿**（取消 / Esc / 遮罩点击 / 成功后共用）。
  // 提交进行中不允许关闭产生新状态：直接返回（按钮已禁用，防御 Enter 路径）。
  function closeProjectDialog() {
    if (!els.projectCreateMask) return;
    if (projectDialog.inFlight) return;
    projectDialog.open = false;
    projectDialog.mode = null;
    projectDialog.slides = [];
    projectDialog.idemKey = null;
    projectDialog.idemFingerprint = "";
    projectDialog.parentPid = null;
    els.projectCreateMask.hidden = true;
    pcdShowError("");
    var back = projectDialog.lastFocusEl;
    projectDialog.lastFocusEl = null;
    if (back && typeof back.focus === "function") {
      try { back.focus(); } catch (e) { /* 触发按钮可能已移除 */ }
    }
  }

  // 定位新文件夹（成功后的「locate pid」）：在当前文件夹内创建的——原地重渲
  // （新文件夹卡直接可见）；否则切到该文件夹。导航结束写位置记忆（§2）。
  function locateProject(pid) {
    if (!pid || !fbProject(pid)) return;
    var parent = fbParentOf(fbProject(pid));
    if ((parent || null) === (fbState.folder || null)) {
      renderFolderBrowser();
      return;
    }
    fbState.pages[fbState.folder] = fbState.page;
    fbState.folder = pid;
    fbState.page = 0;
    renderFolderBrowser();
    fbNavTouched = true;
    fbMemorySave();
  }

  function ensureProjectIdemKey(payloadFp) {
    if (projectDialog.idemKey && projectDialog.idemFingerprint === payloadFp) {
      return projectDialog.idemKey;  // 同一草稿重试：复用（响应丢失幂等收口）
    }
    projectDialog.idemKey = uuid();
    projectDialog.idemFingerprint = payloadFp;
    return projectDialog.idemKey;
  }

  function submitProjectDialog() {
    if (!projectDialog.open || projectDialog.inFlight) return;  // R4 双保险
    if (!els.pcdName) return;
    var name = (els.pcdName.value || "").trim();
    if (!name) { pcdShowError(t("newproj.need.name")); try { els.pcdName.focus(); } catch (e) {} return; }
    var note = (els.pcdNote && els.pcdNote.value) || "";
    // slides 永远显式：空数组=空项目（R3），无任何隐式回退
    var slides = projectDialog.slides.slice();
    var fp = projectDialogFingerprint(name, note, slides, projectDialog.parentPid);
    var idemKey = ensureProjectIdemKey(fp);
    projectDialog.inFlight = true;
    pcdShowError("");
    els.pcdConfirm.disabled = true;
    els.pcdConfirm.textContent = t("pcd.creating.folder");
    var headers = { "Content-Type": "application/json", "Idempotency-Key": idemKey };
    // P2 合同 §3.2：ID 通道发 slide_ids（草稿键是行操作键 slide_id；旧后端
    // 回落 slides 名数组）；改版附加 parent_project_id（当前文件夹）
    apiFetch("/api/project/create", {
      method: "POST",
      headers: headers,
      body: JSON.stringify(Object.assign(
        { name: name, note: note },
        projectDialog.parentPid ? { parent_project_id: projectDialog.parentPid } : {},
        slideRefsPayload(slides))),
    })
      .then(function (r) {
        return r.json().then(function (j) {
          return { ok: r.ok, status: r.status, body: j };
        }, function () {
          return { ok: r.ok, status: r.status, body: null };
        });
      })
      .then(function (res) {
        if (!res.ok) {
          var msg = (res.body && res.body.error) || t("newproj.create.fail");
          throw new Error(msg);
        }
        return res.body || {};
      })
      .then(function (created) {
        toast(t("newproj.created"), "success");
        // 成功：关闭 + 清空草稿 + 勾选清零 + 重载并定位
        projectDialog.inFlight = false;
        projectDialog.open = false;
        projectDialog.slides = [];
        projectDialog.idemKey = null;
        projectDialog.idemFingerprint = "";
        if (els.projectCreateMask) els.projectCreateMask.hidden = true;
        if (els.pcdConfirm) {
          els.pcdConfirm.disabled = false;
          els.pcdConfirm.textContent = t("pcd.confirm.folder");
        }
        reloadProjectsAndUnfiled().then(function () {
          locateProject(created && (created.pid || created.id));
        }).catch(function () {
          // 列表刷新失败不影响创建结果（对话框已关闭、草稿已清）
          locateProject(created && (created.pid || created.id));
        });
      })
      .catch(function (e) {
        // 失败：保留草稿与 idemKey（同草稿重试复用），恢复按钮
        projectDialog.inFlight = false;
        if (els.pcdConfirm) {
          els.pcdConfirm.disabled = false;
          els.pcdConfirm.textContent = t("pcd.confirm.folder");
        }
        pcdShowError(t("newproj.create.fail2", { e: (e && e.message) ? e.message : e }));
      });
  }

  // 对话框焦点圈闭（Tab 不落回背景）：在最后一个可聚焦元素上 Tab 回到第一个。
  // 注意 els 表是 camelCase 键（els.pcdClose…），不能用字面 id 索引——
  // 旧写法 els["pcd-close"] 全为 undefined → focusables 恒空 → 圈闭失效。
  function pcdFocusables() {
    var list = [els.pcdClose, els.pcdName, els.pcdNote, els.pcdCancel, els.pcdConfirm];
    var out = [];
    list.forEach(function (el) {
      if (el && !(el.hidden)) out.push(el);
    });
    return out;
  }

  function handleProjectDialogKeydown(e) {
    if (!projectDialog.open) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeProjectDialog();
      return;
    }
    if (e.key === "Tab") {
      var focusables = pcdFocusables();
      if (!focusables.length) return;
      var active = document.activeElement;
      var first = focusables[0], last = focusables[focusables.length - 1];
      var inDialog = focusables.indexOf(active) >= 0;
      if (e.shiftKey && (!inDialog || active === first)) {
        e.preventDefault();
        try { last.focus(); } catch (err) {}
      } else if (!e.shiftKey && (!inDialog || active === last)) {
        e.preventDefault();
        try { first.focus(); } catch (err) {}
      }
    }
  }

  // ---------- 编辑项目 ----------
  function editProject(p) {
    var name = prompt(t("rename.name.prompt"), p.name || "");
    if (name == null) return;
    var note = prompt(t("rename.note.prompt"), p.note || "");
    if (note == null) return;
    apiFetch("/api/project/" + encodeURIComponent(p.pid), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), note: note }),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("rename.update.fail")); });
        return r.json();
      })
      .then(function () { toast(t("common.updated"), "success"); reloadProjectsAndUnfiled(); })
      .catch(function (e) { toast(t("rename.update.fail2", { e: e.message }), "error"); });
  }

  // ---------- 删除项目 ----------
  // 改版后唯一入口是文件夹 ⋯ 删除（已弹「子文件夹回根、切片不删」的专用
  // 确认）——skipConfirm 跳过旧版「删除项目」二次确认（DEF-1：双层 confirm）。
  function deleteProject(p, opts) {
    opts = opts || {};
    if (!opts.skipConfirm && !confirm(t("delproj.confirm", { name: (p.name || "") }))) return;
    apiFetch("/api/project/" + encodeURIComponent(p.pid), { method: "DELETE" })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("delproj.fail")); });
        return r.json();
      })
      .then(function () { toast(t("delproj.deleted"), "success"); reloadProjectsAndUnfiled(); })
      .catch(function (e) { toast(t("delproj.fail2", { e: e.message }), "error"); });
  }

  // =========================================================================
  // 切片选择器（添加切片到项目）
  // =========================================================================
  // mode="share"：选择切片…（§5.1）——同一选择器，确认改为回填分享浮层目标
  function openSlidePicker(pid, pname, opts) {
    opts = opts || {};
    pickerCtx.targetPid = pid;
    pickerCtx.selected = {};
    pickerCtx.mode = opts.mode === "share" ? "share" : "add";
    els.pickerTitleText.textContent = pickerCtx.mode === "share"
      ? t("sb.share.pick")
      : (pname ? t("picker.title.with", { name: pname }) : t("picker.title"));
    els.pickerList.innerHTML = "";
    allSlides.forEach(function (s) {
      var row = document.createElement("label");
      row.className = "picker-item";
      var cb = document.createElement("input");
      cb.type = "checkbox";
      // 选择器键 = 行操作键（slide_id；旧后端回落名）
      cb.value = slideRefOf(s);
      cb.addEventListener("change", function () {
        pickerCtx.selected[cb.value] = cb.checked;
        updatePickerCount();
      });
      row.appendChild(cb);
      var info = document.createElement("span");
      info.className = "pi-info";
      var dispName = slideDisplayName(s);
      var nameHtml = (dispName && dispName !== s.name)
        ? '<span class="pi-alias">' + esc(dispName) + "</span>" +
          '<span class="alias-filename">' + esc(truncateMiddle(s.name, 24)) + "</span>"
        : esc(truncateMiddle(s.name, 30));
      info.innerHTML = '<span class="pi-name">' + nameHtml + "</span>" +
        '<span class="pi-meta">' + slideMetaTags(s) + "</span>";
      row.appendChild(info);
      els.pickerList.appendChild(row);
    });
    updatePickerCount();
    els.pickerMask.style.display = "flex";
  }

  function updatePickerCount() {
    var n = 0;
    Object.keys(pickerCtx.selected).forEach(function (k) { if (pickerCtx.selected[k]) n++; });
    els.pickerSelectedCount.textContent = t("picker.selected", { n: n });
  }

  function closeSlidePicker() {
    els.pickerMask.style.display = "none";
    pickerCtx.targetPid = null;
    pickerCtx.selected = {};
    pickerCtx.mode = "add";
  }

  function confirmSlidePicker() {
    var refs = Object.keys(pickerCtx.selected).filter(function (k) { return pickerCtx.selected[k]; });
    if (refs.length === 0) { toast(t("picker.need.slide"), "error"); return; }
    if (pickerCtx.mode === "share") {
      // 分享多选：回填浮层目标并重新打开浮层（选择器遮罩打开期间浮层按
      // 「点击外部」规则收起，确认后回到分享浮层直接可点「分享选中切片」）
      sharePendingSlides = refs;
      updateShareTargetLine();
      toast(t("sb.share.picked.toast", { n: refs.length }), "info");
      closeSlidePicker();
      if (sharePopCtl) sharePopCtl.open();
      return;
    }
    var pid = pickerCtx.targetPid;
    if (!pid) return;
    // P2 合同 §3.2：ID 通道发 slide_ids（picker 键=行操作键）；旧后端 slides(名)
    apiFetch("/api/project/" + encodeURIComponent(pid) + "/slides", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(slideRefsPayload(refs)),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("picker.add.fail")); });
        return r.json();
      })
      .then(function () {
        toast(t("picker.added", { n: refs.length }), "success");
        closeSlidePicker();
        reloadProjectsAndUnfiled();
      })
      .catch(function (e) { toast(t("picker.add.fail2", { e: e.message }), "error"); });
  }

  // =========================================================================
  // 分享功能
  // =========================================================================
  function getExpiresHours() {
    var v = els.shareExpiresSelect.value;
    if (v === "custom") {
      var c = parseFloat(els.shareExpiresCustom.value);
      if (!isFinite(c) || c <= 0) return null;
      return c;
    }
    return parseFloat(v);
  }

  // 读取"标记尺寸"下拉值 → roi_sizes 数组（[6,6.5]/[6]/[6.5]）
  function getShareRoiSizes() {
    var v = els.shareRoiSizeSelect ? els.shareRoiSizeSelect.value : "both";
    if (v === "6") return [6];
    if (v === "6.5") return [6.5];
    return [6, 6.5];
  }

  // 读取分享链接权限（docs §8.3：显式选择，view 为基线，不无提示默认 annotate）
  function getSharePermissions() {
    var perms = ["view"];
    if (els.sharePermAnnotate && els.sharePermAnnotate.checked) { perms.push("annotate"); }
    if (els.sharePermDownload && els.sharePermDownload.checked) { perms.push("download"); }
    return perms;
  }

  // roi_sizes 数组 → 人类可读标签（用于分享列表 meta）
  function roiSizesLabel(sizes) {
    if (!sizes || !sizes.length) return "6/6.5mm";
    var set = {};
    sizes.forEach(function (s) { set[Number(s)] = true; });
    if (set[6] && set[6.5]) return "6/6.5mm";
    if (set[6.5]) return t("share.size.only.6.5");
    if (set[6]) return t("share.size.only.6");
    return "6/6.5mm";
  }

  // 统一创建分享入口：refs 为要分享的切片操作键数组（data-slide-id；旧后端
  // =名）。P2 合同 §4：ID 通道发 slide_ids；旧后端回落 slides(名)。
  function doCreateShare(refs) {
    if (!refs || refs.length === 0) { toast(t("share.need.slide"), "error"); return; }
    var hours = getExpiresHours();
    if (hours == null) { toast(t("share.need.hours"), "error"); return; }
    var roiSizes = getShareRoiSizes();
    var permissions = getSharePermissions();
    // 升级 C（§6.4）：矩形策略档位（preset_only 缺省；custom 显式选择）
    var rectPolicy = (els.shareRectPolicySelect && els.shareRectPolicySelect.value)
      || "preset_only";
    els.shareCreateBtn.disabled = true;
    els.shareCreateBtn.textContent = t("share.creating");
    apiFetch("/api/share/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({
        expires_hours: hours, roi_sizes: roiSizes,
        permissions: permissions, rect_policy: rectPolicy,
      }, slideRefsPayload(refs))),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || (t("share.create.fail") + " " + r.status)); });
        return r.json();
      })
      .then(function (data) {
        els.shareResult.style.display = "flex";
        els.shareResultUrl.value = data.url;
        copyText(data.url);
        toast(t("share.created"), "success");
        sharePendingSlides = null;
        updateShareTargetLine();
        reloadShares();
      })
      .catch(function (e) { toast(t("share.create.fail2", { s: e.message }), "error"); })
      .finally(function () {
        els.shareCreateBtn.disabled = false;
        els.shareCreateBtn.textContent = t("sb.share.create");
      });
  }

  // 分享文件夹（项目 DTO 的 slides 是名数组；ID 通道逐名解析成 slide_id 作
  // 操作键，列表外资产保持名——payload 构建时按通道裁决）。改版后入口 =
  // 文件夹「⋯ → 分享此文件夹」：预填目标并打开顶栏分享浮层（§5.1）。
  function shareProject(p) {
    var slides = p.slides || [];
    if (slides.length === 0) { toast(t("share.project.empty"), "error"); return; }
    sharePendingSlides = slides.map(function (n) {
      var s = findSlideInfo(n);
      return (s && s.slide_id) || n;
    });
    els.shareCreateBtn.textContent = t("share.project.btn", { name: (p.name || ""), n: slides.length });
    toast(t("share.project.selected.tip", { n: slides.length }), "info");
    els.shareResult.style.display = "none";
    updateShareTargetLine();
    if (sharePopCtl) sharePopCtl.open();
  }

  // 分享浮层「分享选中切片」：优先浮层预填目标（当前切片 / 选择切片… /
  // 分享此文件夹），否则回落当前打开的切片
  function onShareCreateClick() {
    var slides;
    if (sharePendingSlides) {
      slides = sharePendingSlides;
    } else if (state.slide) {
      slides = [activeSlideRef()];
    } else {
      slides = [];
    }
    doCreateShare(slides);
  }

  // 分享浮层目标摘要行（当前切片 / N 张切片 / 未选择）
  function updateShareTargetLine() {
    if (!els.shareTargetLine) return;
    if (sharePendingSlides && sharePendingSlides.length) {
      els.shareTargetLine.textContent = t("sb.share.target.slides", { n: sharePendingSlides.length });
    } else if (state.slide) {
      els.shareTargetLine.textContent = t("sb.share.target.slide",
        { name: slideDisplayName(state.slide) || state.slide.name });
    } else {
      els.shareTargetLine.textContent = t("sb.share.target.none");
    }
  }

  function renderShareList(shares) {
    allSharesCache = shares || [];
    els.shareList.innerHTML = "";
    if (!shares || shares.length === 0) {
      var empty = document.createElement("div");
      empty.className = "share-empty";
      empty.textContent = t("share.empty");
      els.shareList.appendChild(empty);
      return;
    }
    shares.forEach(function (sh) {
      var row = document.createElement("div");
      row.className = "share-row-item";

      // 状态彩色圆点
      var dot = document.createElement("span");
      dot.className = "sr-status-dot " + sh.status;
      dot.title = sh.status === "active" ? t("share.status.active") :
                  (sh.status === "expired" ? t("share.status.expired") : t("share.status.revoked"));
      row.appendChild(dot);

      // 中部：token（等宽） + 副行 meta
      var mid = document.createElement("div");
      mid.className = "sr-mid";
      var shortTok = sh.token.length > 8 ? sh.token.slice(0, 8) : sh.token;
      var tokEl = document.createElement("span");
      tokEl.className = "sr-token";
      tokEl.textContent = shortTok;
      tokEl.title = sh.url;
      mid.appendChild(tokEl);

      var meta = document.createElement("span");
      meta.className = "sr-meta";
      var slidesTxt = t("share.slides.tip", { n: sh.slides.length, list: sh.slides.join(", ") });
      meta.innerHTML =
        '<span title="' + esc(slidesTxt) + '">' + sh.slides.length + " " + esc(t("share.slides.unit")) + "</span>" +
        '<span class="sr-sep">·</span>' +
        "<span>" + esc(t("share.expires", { e: fmtExpire(sh.expires_at) })) + "</span>" +
        '<span class="sr-sep">·</span>' +
        "<span>" + esc(t("share.rois", { n: (sh.roi_count || 0) })) + "</span>" +
        '<span class="sr-sep">·</span>' +
        "<span>" + esc(roiSizesLabel(sh.roi_sizes)) + "</span>";
      mid.appendChild(meta);
      row.appendChild(mid);

      // 操作按钮（hover 浮现）
      var ops = document.createElement("div");
      ops.className = "sr-ops";
      var copyBtn = document.createElement("button");
      copyBtn.className = "sr-btn sr-copy";
      copyBtn.textContent = "⧉";
      copyBtn.title = t("share.copy.title");
      copyBtn.addEventListener("click", function () { copyText(sh.url); });
      ops.appendChild(copyBtn);
      var revBtn = document.createElement("button");
      revBtn.className = "sr-btn sr-revoke";
      revBtn.textContent = "⊘";
      revBtn.title = t("share.revoke.title");
      revBtn.addEventListener("click", function () { revokeShare(sh.token); });
      if (sh.status !== "active") revBtn.disabled = true;
      ops.appendChild(revBtn);
      row.appendChild(ops);
      els.shareList.appendChild(row);
    });
  }

  function revokeShare(token) {
    if (!confirm(t("share.revoke.confirm"))) return;
    apiFetch("/api/share/revoke", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token }),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || (t("share.revoke.fail") + " " + r.status)); });
        return r.json();
      })
      .then(function () { toast(t("share.revoked"), "success"); reloadShares(); })
      .catch(function (e) { toast(t("share.revoke.fail2", { s: e.message }), "error"); });
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text)
        .then(function () { toast(t("share.copied"), "success"); })
        .catch(function () { fallbackCopy(text); });
    } else { fallbackCopy(text); }
  }
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); toast(t("share.copied"), "success"); }
    catch (e) { toast(t("share.copy.fail"), "error"); }
    document.body.removeChild(ta);
  }
  function fmtExpire(ts) {
    if (!ts) return "-";
    var d = new Date(ts * 1000);
    var p = function (n) { return n < 10 ? "0" + n : n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  // =========================================================================
  // 侧栏开合控制器（升级 A §4.1/§4.2）
  // -------------------------------------------------------------------------
  // 桌面（>768px）：默认收起；顶部 #menu-btn 在展开/收起间切换。偏好按
  // 「站点:账号」维度写 localStorage（pt.sb.v1| 前缀，与通道配色 pt.rc.v1 的
  // userScope 同一身份口径）；读取失败/无偏好默认收起；存储不可用不阻塞。
  // 手机（≤768px）：维持侧滑抽屉，默认关闭；桌面偏好不把抽屉自动打开。
  // 纯逻辑集中在 createSidebarController(deps)：DOM/mq/storage 经 deps 注入，
  // vitest（tests/js/sidebar-layout.test.ts）以假元素驱动真实决策逻辑。
  // =========================================================================
  var SIDEBAR_PREF_PREFIX = "pt.sb.v1|";
  var SB_MOBILE_QUERY = "(max-width: 768px)";

  function sidebarPrefKey(scope) {
    return SIDEBAR_PREF_PREFIX + String(scope || "anonymous");
  }
  // 解析存储的偏好：结构不符/非 JSON 一律返回 null（调用方回落默认收起）
  function parseSidebarPref(raw) {
    if (raw == null) return null;
    try {
      var v = JSON.parse(raw);
      if (v && typeof v === "object" && typeof v.collapsed === "boolean") {
        return { collapsed: v.collapsed };
      }
    } catch (e) { /* 损坏数据视为无偏好 */ }
    return null;
  }
  function readSidebarPref(storage, scope) {
    if (!storage || typeof storage.getItem !== "function") return null;
    try {
      return parseSidebarPref(storage.getItem(sidebarPrefKey(scope)));
    } catch (e) { return null; }  // 隐私模式等 getItem 抛错：无偏好
  }
  function writeSidebarPref(storage, scope, collapsed) {
    if (!storage || typeof storage.setItem !== "function") return;
    try {
      storage.setItem(sidebarPrefKey(scope),
        JSON.stringify({ collapsed: !!collapsed, t: Date.now() }));
    } catch (e) { /* 配额/隐私模式写失败：仅失去持久化 */ }
  }

  function createSidebarController(deps) {
    var sidebar = deps.sidebar;
    var mask = deps.sidebarMask;
    var btn = deps.menuBtn;
    var collapsed = true;      // 桌面意图态（首次进入默认收起）
    var userTouched = false;   // 身份到位回填偏好时不得覆盖用户已做的切换

    function scopeName() {
      return (typeof deps.scope === "function") ? deps.scope() : (deps.scope || "anonymous");
    }
    function isMobile() {
      return !!(deps.mq && typeof deps.mq.matches === "boolean" && deps.mq.matches);
    }
    // 按钮 a11y 状态：桌面 expanded=侧栏可见；手机 expanded=抽屉打开
    function setBtnState(expanded) {
      if (!btn || !btn.setAttribute) return;
      btn.setAttribute("aria-expanded", String(!!expanded));
      var label = t(expanded ? "tb.sidebar.collapse" : "tb.sidebar.expand");
      btn.setAttribute("aria-label", label);
      btn.title = label;
    }
    function applyDesktop() {
      if (sidebar && sidebar.classList) sidebar.classList.toggle("collapsed", collapsed);
      setBtnState(!collapsed);
      if (typeof deps.onLayoutChange === "function") deps.onLayoutChange();
    }
    function drawerOpen() {
      return !!(sidebar && sidebar.classList && sidebar.classList.contains("open"));
    }
    function applyDrawer(open) {
      open = !!open;
      if (sidebar && sidebar.classList) sidebar.classList.toggle("open", open);
      if (mask && mask.classList) mask.classList.toggle("open", open);
      setBtnState(open);
      // a11y：抽屉关闭后焦点若留在抽屉内（将被移出视口/不可达），回到触发按钮
      var doc = deps.doc;
      if (!open && doc && doc.activeElement && sidebar && sidebar.contains &&
          sidebar.contains(doc.activeElement) && btn && typeof btn.focus === "function") {
        btn.focus();
      }
      if (typeof deps.onLayoutChange === "function") deps.onLayoutChange();
    }

    return {
      // 启动：按当前断点应用布局。手机抽屉固定默认关闭（桌面偏好不外溢）；
      // 桌面读偏好（含启动时身份未知的 official:local 维度），无偏好/读取失败
      // 默认收起（§4.1）
      init: function () {
        if (btn && btn.setAttribute) btn.setAttribute("aria-controls", "sidebar");
        if (isMobile()) {
          applyDrawer(false);
        } else {
          var pref = readSidebarPref(deps.storage, scopeName());
          if (pref) collapsed = pref.collapsed;
          applyDesktop();
        }
      },
      isMobile: isMobile,
      isDesktopCollapsed: function () { return collapsed; },
      isDrawerOpen: drawerOpen,
      // #menu-btn 点击：桌面=切换收起并持久化；手机=开关抽屉（不写偏好）
      toggle: function () {
        if (isMobile()) { applyDrawer(!drawerOpen()); return; }
        collapsed = !collapsed;
        userTouched = true;
        applyDesktop();
        writeSidebarPref(deps.storage, scopeName(), collapsed);
      },
      closeDrawer: function () {
        if (drawerOpen()) applyDrawer(false);
      },
      // /api/auth/info 到位后按真实身份重读偏好（用户已手动操作则不覆盖）
      onScopeReady: function () {
        if (userTouched || isMobile()) return;
        var pref = readSidebarPref(deps.storage, scopeName());
        if (pref && pref.collapsed !== collapsed) {
          collapsed = pref.collapsed;
          applyDesktop();
        }
      },
      // 断点切换：清理手机遮罩/抽屉与桌面收起类的残留，恢复当前设备布局状态
      onBreakpointChange: function () {
        if (sidebar && sidebar.classList) sidebar.classList.remove("open", "collapsed");
        if (mask && mask.classList) mask.classList.remove("open");
        if (isMobile()) applyDrawer(false);
        else applyDesktop();
      },
      // 空态「选择切片」：展开侧栏（桌面同时持久化为展开）并聚焦搜索框（§4.1）
      expandAndFocusSearch: function () {
        if (isMobile()) {
          if (!drawerOpen()) applyDrawer(true);
        } else if (collapsed) {
          collapsed = false;
          userTouched = true;
          applyDesktop();
          writeSidebarPref(deps.storage, scopeName(), collapsed);
        }
        if (typeof deps.focusSearch === "function") deps.focusSearch();
      },
      // 账户设置入口（§3.5）：只展开侧栏/开抽屉（不抢搜索框焦点），
      // 随后由调用方滚动到既有改密/改绑卡片区
      expand: function () {
        if (isMobile()) {
          if (!drawerOpen()) applyDrawer(true);
          return;
        }
        if (collapsed) {
          collapsed = false;
          userTouched = true;
          applyDesktop();
          writeSidebarPref(deps.storage, scopeName(), collapsed);
        }
      },
      // 语言切换后同步按钮文案/aria（状态不变，仅 label）
      refreshButton: function () {
        setBtnState(isMobile() ? drawerOpen() : !collapsed);
      },
    };
  }

  // 存储访问兜底：隐私模式下访问 window.localStorage 本身可能抛错
  function safeLocalStorage() {
    try {
      var s = window.localStorage;
      if (s && typeof s.getItem === "function") { s.getItem("pt.sb.probe"); return s; }
    } catch (e) { /* 不可用：偏好不持久化，页面照常启动 */ }
    return null;
  }

  var aiPanelCtrl = null;   // init() 里创建（initAIPanelController 装配依赖）

  function initAIPanelController() {
    // 外框拖动/缩放由共享模块 ai-panel-shell.js 提供（正式版与 Demo 同源）；
    // 模块缺失（旧缓存/静态资源故障）时保持默认 CSS 锚定，面板照常可用。
    var shell = window.HP_AI_PANEL;
    if (!shell || typeof shell.createAIPanelController !== "function") return;
    var mq = window.matchMedia ? window.matchMedia(SB_MOBILE_QUERY) : null;
    aiPanelCtrl = shell.createAIPanelController({
      panel: els.aiPanel,
      header: els.aiPanelHeader,
      handle: els.aiPanelResize,
      frame: els.viewerWrap,
      mq: mq,
      storage: safeLocalStorage(),
      scope: userScope,
      doc: document,
      win: window,
    });
    aiPanelCtrl.init();
    // ⋯ 菜单「重置面板位置」（§3.1）：恢复默认锚定并清除偏好（弹层关闭由
    // ai-panel-chrome.js 处理）
    if (els.aiResetPosition) {
      els.aiResetPosition.addEventListener("click", function () {
        aiPanelCtrl.resetPosition();
      });
    }
    if (mq) {
      var onMqChangeAip = function () { if (aiPanelCtrl) aiPanelCtrl.onBreakpointChange(); };
      if (typeof mq.addEventListener === "function") mq.addEventListener("change", onMqChangeAip);
      else if (typeof mq.addListener === "function") mq.addListener(onMqChangeAip);
    }
  }


  var sidebarCtrl = null;   // init() 里创建（createSidebarController 装配真实依赖）

  // 断点判定（控制器外的兜底路径沿用同一媒体查询口径）
  function isMobileWidth() {
    if (sidebarCtrl && typeof sidebarCtrl.isMobile === "function") return sidebarCtrl.isMobile();
    return !!(window.matchMedia && window.matchMedia(SB_MOBILE_QUERY).matches);
  }

  // ---------- 无切片空态（升级 A §4.1） ----------
  // 侧栏默认收起后，空态保留明显的「选择切片」入口；切片打开后隐藏。
  function updateViewerEmptyState() {
    if (els.viewerEmpty) els.viewerEmpty.hidden = !!state.slide;
  }

  // ---------- 侧栏宽度变化 → Viewer resize 链（升级 A §4.2，几何验收核心） ----------
  // 侧栏/抽屉宽度变化必须让 OSD 重算 viewport 再重绘叠加层，不能沿用旧容器
  // 尺寸的画布坐标。OSD 5 的容器 ResizeObserver 在下一帧执行
  // viewport.resize(preserveImageSizeOnResize) 并 panTo(原中心)——图像中心与
  // 缩放保留、绝不 goHome；随后触发 viewer "resize" 事件 → 重绘标注/AI overlay。
  // 这里在布局落定（下一帧）后补画布背衬尺寸同步与 ROI 框/底图缩略图对位。
  function syncViewerLayoutNow() {
    // forceResize 兜底：容器尺寸未越过 OSD 内部阈值或 observer 时机抖动时强制重算
    try { if (viewer && viewer.forceResize) viewer.forceResize(); } catch (e) { /* 忽略 */ }
    resizeAnnoCanvas();
    redrawAnnoCanvas();
    updateRoiOverlay();
    syncBaseThumb();
    // 侧栏开合改变命中区几何：抽出层立即收回（改版第四轮 §1.1）
    fbPulloutOnLayoutChange();
  }
  function syncViewerLayoutAfterSidebar() {
    // 等一帧：flex 布局/宽度过渡先落定，再按新容器尺寸同步
    if (typeof window.requestAnimationFrame === "function") {
      window.requestAnimationFrame(function () { syncViewerLayoutNow(); });
    } else {
      syncViewerLayoutNow();
    }
  }

  // ---------- 顶栏搜索浮层（改版 2026-10-08，§5.4） ----------
  // 替换旧侧栏内联过滤：浮层挂在顶栏「搜索」按钮下（bindToolbarPop 统一开合/
  // 外点关闭/Esc）。输入框由打开时创建（沿用 slide-search-autofill-bug 的
  // 防自动填充方案：首屏 DOM 无搜索输入框 + autocomplete off + 各管理器忽略
  // 标记）。匹配 /api/slides 全量（显示名/别名/原文件名），点击结果 → 左栏
  // 切到所在文件夹并翻页 → openSlide → 关浮层；Esc 关闭并把焦点还给按钮。
  var tbSearchState = { input: null, results: [], page: 0, pageSize: 6, totalPages: 1 };
  var searchPopCtl = null;
  var searchThumbSeq = 0;

  function ensureSearchInput() {
    if (!els.tbSearchArea || tbSearchState.input) return;
    var input = document.createElement("input");
    input.type = "search";
    input.id = "tb-search-input";
    input.className = "slide-search tb-search-input";
    // 防自动填充（辅助措施）：非账号含义的字段名 + autocomplete off + 各
    // 密码管理器忽略标记；主要保障是首屏没有输入框
    input.name = "slide-search-pop";
    input.setAttribute("autocomplete", "off");
    input.setAttribute("data-lpignore", "true");
    input.setAttribute("data-1p-ignore", "");
    input.setAttribute("data-bwignore", "");
    input.setAttribute("data-form-type", "other");
    input.setAttribute("placeholder", t("tb.search.ph"));
    input.setAttribute("aria-label", t("tb.search.aria"));
    input.addEventListener("input", function () { renderSearchResults(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        var first = els.tbSearchResults && els.tbSearchResults.querySelector(".tb-search-result");
        if (first && typeof first.focus === "function") { try { first.focus(); } catch (err) {} }
      }
    });
    els.tbSearchArea.innerHTML = "";
    els.tbSearchArea.appendChild(input);
    tbSearchState.input = input;
  }

  function refreshSearchTexts() {
    if (tbSearchState.input) {
      tbSearchState.input.setAttribute("placeholder", t("tb.search.ph"));
      tbSearchState.input.setAttribute("aria-label", t("tb.search.aria"));
    }
  }

  function renderSearchResults() {
    if (!els.tbSearchResults) return;
    var q = tbSearchState.input ? tbSearchState.input.value : "";
    tbSearchState.results = fbSearchSlides(q);
    tbSearchState.page = 0;
    tbSearchState.totalPages = 1;
    els.tbSearchResults.innerHTML = "";
    if (!q || !String(q).trim()) return; // 空查询：不渲染结果区
    if (!tbSearchState.results.length) {
      var empty = document.createElement("div");
      empty.className = "tb-search-empty";
      empty.textContent = t("tb.search.empty");
      els.tbSearchResults.appendChild(empty);
      return;
    }
    var thumbSeq = ++searchThumbSeq;
    tbSearchState.results.forEach(function (s) {
      var sid = slideRefOf(s);
      var row = document.createElement("button");
      row.type = "button";
      row.className = "tb-search-result";
      row.dataset.slideId = sid;
      var img = document.createElement("img");
      img.alt = "";
      img.src = slideThumbnailUrl(sid);
      img.addEventListener("error", function () {
        img.classList.add("tb-thumb-error");
        img.removeAttribute("src");
      });
      row.appendChild(img);
      var info = document.createElement("span");
      info.className = "tsr-info";
      var disp = slideDisplayName(s);
      var nameEl = document.createElement("span");
      nameEl.className = "tsr-name";
      nameEl.textContent = disp || s.name || sid;
      info.appendChild(nameEl);
      // 同名消歧：位置 + slide_id 末 6 位（§5.4）
      var locEl = document.createElement("small");
      locEl.className = "tsr-loc";
      var locText = fbSlideLocationText(s);
      var id6 = sid && s.slide_id ? String(s.slide_id).slice(-6) : "";
      locEl.textContent = locText + (id6 ? " · " + id6 : "");
      info.appendChild(locEl);
      row.appendChild(info);
      row.addEventListener("click", function () {
        var slideRef = sid;
        var sinfo = findSlideInfo(slideRef) || s;
        closeSearchPop();
        // 左栏切到首个所在文件夹（或根目录）并翻到其所在叠 → 打开切片
        if (sidebarCtrl && sidebarCtrl.isDesktopCollapsed && sidebarCtrl.isDesktopCollapsed()) {
          sidebarCtrl.expand();
        }
        fbLocateSlide(sinfo);
        openSlide(slideRef);
      });
      els.tbSearchResults.appendChild(row);
    });
    void thumbSeq;
  }

  function closeSearchPop() {
    if (searchPopCtl) searchPopCtl.close();
  }

  // ---------- 移动端上下文动作条显隐 ----------
  // ROI 模式或箭头/描图绘制模式任一激活时，显示底部主栏上方的上下文条
  // （标注人输入 + 保存标记/保存图片）。桌面端不受影响（display:contents）。
  function updateCtxBar() {
    var on = state.roiMode != null || state.drawMode != null;
    document.body.classList.toggle("ctx-on", on);
  }

  // ---------- ⋯ 溢出菜单（§3.3 宽度分组的统一折叠目标；装 AI 副本/Reset/
  //   显示全部标记，以及按断点搬入的视图/标注/保存等真实 DOM 节点） ----------
  // 外点关闭走 document 级监听：#tbb-more-mask（z 490）会盖住 #app-header
  // （z 10）导致菜单项点击被 mask 吞掉（只关菜单不触发动作），故不再展示
  // mask（元素保留，移动端 CSS 不受影响）。
  function bindTbbMore() {
    if (!els.tbbMoreBtn || !els.tbbMore) return;
    var mask = $("tbb-more-mask");
    function closeMore() {
      els.tbbMore.classList.remove("open");
      if (mask) mask.classList.remove("open");
      els.tbbMoreBtn.setAttribute("aria-expanded", "false");
      // 菜单关闭后再按主行宽度收放，浮层不参与主行溢出预算。
      applyToolbarTier();
    }
    function openMore() {
      // 同一时间至多一个工具栏浮层：打开 ⋯ 前收起其它浮层
      toolbarPopClosers.forEach(function (fn) { fn(); });
      els.tbbMore.classList.add("open");
      els.tbbMoreBtn.setAttribute("aria-expanded", "true");
    }
    closeTbbMoreMenu = closeMore;
    els.tbbMoreBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (els.tbbMore.classList.contains("open")) { closeMore(); } else { openMore(); }
    });
    // 点击菜单外（viewer / 侧栏 / 工具栏其它控件）即关闭；菜单内部点击照常生效
    document.addEventListener("click", function (e) {
      if (!els.tbbMore.classList.contains("open")) return;
      var tgt = e.target;
      if (tgt && tgt.closest &&
          (tgt.closest("#tbb-more") || tgt.closest("#tbb-more-btn"))) return;
      closeMore();
    });
    if (mask) mask.addEventListener("click", closeMore);
    // Esc 关菜单（HIG：弹出层必须可键盘退出）
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && els.tbbMore.classList.contains("open")) closeMore();
    });
    // 菜单项点击后收起菜单（各自的原处理逻辑不变）
    els.tbbMore.addEventListener("click", function (e) {
      if (e.target && e.target.closest && e.target.closest(".tool-btn")) closeMore();
    });
    // ⋯ 菜单里的 AI 钮：转发给主 AI 钮（打开/关闭 AI 面板）
    if (els.tbbMoreAi) {
      els.tbbMoreAi.addEventListener("click", function () {
        if (els.aiBtn && !els.aiBtn.disabled) els.aiBtn.click();
      });
    }
  }

  // =========================================================================
  // 标注画布层（rect/arrow/freehand 统一绘制）
  // =========================================================================
  var annoCtx = null;
  var annoCanvasSize = { width: 0, height: 0 };
  var annoCanvasHasContent = false;

  function resizeAnnoCanvas() {
    var c = els.annoCanvas;
    if (!c || !viewer) return;
    var rect = viewer.container.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    c.width = Math.max(1, Math.floor(rect.width * dpr));
    c.height = Math.max(1, Math.floor(rect.height * dpr));
    c.style.width = rect.width + "px";
    c.style.height = rect.height + "px";
    annoCtx = c.getContext("2d");
    annoCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    annoCanvasSize = { width: rect.width, height: rect.height };
    annoCanvasHasContent = false;
  }

  // 把图像坐标转为画布层屏幕坐标（自带旋转支持）
  function imgToCanvas(ix, iy) {
    if (!viewer || !viewer.viewport) return { x: 0, y: 0 };
    var p = viewer.viewport.imageToViewerElementCoordinates(
      new OpenSeadragon.Point(ix, iy));
    return { x: p.x, y: p.y };
  }

  // 当前切片的标注展开为扁平 item 列表（带 label/type/几何）
  // flatItems 为持久缓存（编辑拖动时改本地几何），每次标注刷新时重建
  var flatItems = [];
  function flatAnnoItems() {
    return flatItems;
  }
  function rebuildFlatItems() {
    var out = [];
    if (currentAnnotations) {
      (currentAnnotations.annotations || []).forEach(function (grp) {
        (grp.items || []).forEach(function (it) {
          var copy = {};
          for (var k in it) copy[k] = it[k];
          copy.label = grp.label;
          out.push(copy);
        });
      });
    }
    flatItems = out;
  }

  function redrawAnnoCanvas(viewMoving) {
    var c = els.annoCanvas;
    if (!c || !annoCtx) { if (c) resizeAnnoCanvas(); }
    if (!annoCtx) return;
    var hasAiOverlay = aiOverlay && aiOverlay.length > 0;
    var hasCount = !!countHighlight;
    var hasContent = !!(state.slide && ((state.showAnno && (flatItems.length || editItem)) ||
      (state.drawMode != null && drawPreview) || hasAiOverlay || hasCount));
    // An already-empty canvas has nothing to clear during viewport movement.
    // Clear once when annotations/AI previews are hidden or the slide closes.
    if (!hasContent && !annoCanvasHasContent) return;
    annoCtx.clearRect(0, 0, annoCanvasSize.width, annoCanvasSize.height);
    annoCanvasHasContent = hasContent;
    if (!hasContent) return;
    // OSD exposes isAnimating on Viewer, not Viewport. The explicit frame flag
    // also covers the first animation event before OSD updates its public flag.
    var animating = viewMoving === true || !!(viewer &&
      typeof viewer.isAnimating === "function" && viewer.isAnimating());
    // 拖动编辑中只保留选中项的气泡，其余气泡暂停（视图静止时减少文本重绘）
    var dragging = !!(editDrag && editItem);
    // 已保存标注（focus 过滤：有 focusAnno 时只画它）
    if (state.showAnno) {
      flatAnnoItems().forEach(function (it) {
        if (state.focusAnno && it !== state.focusAnno) return;
        var selected = (editItem === it);
        drawAnnoItem(it, labelColor(it.label), selected, !animating);
      });
    }
    // AI overlay：当前视角 / 历史路径 = 虚线框；局部观察 = 绿色实线。不跟人眼视野走。
    if (hasAiOverlay) {
      aiOverlay.forEach(function (bb) {
        var tl = imgToCanvas(bb.x, bb.y);
        var br = imgToCanvas(bb.x + bb.w, bb.y + bb.h);
        var x = Math.min(tl.x, br.x), y = Math.min(tl.y, br.y);
        var w = Math.abs(br.x - tl.x), h = Math.abs(br.y - tl.y);
        var role = bb.role || "view";
        var fill = AI_OVERLAY_FILL, stroke = AI_OVERLAY_STROKE, halo = AI_OVERLAY_HALO, dash = [7, 4];
        if (role === "path") {
          fill = null;
          stroke = "rgba(255, 149, 0, 0.72)";
          halo = "rgba(0, 0, 0, 0.45)";
          dash = [6, 4];
        } else if (role === "obs") {
          fill = "rgba(52, 199, 89, 0.10)";
          stroke = "#34C759";
          halo = "rgba(0, 0, 0, 0.45)";
          dash = [];
        }
        annoCtx.save();
        if (fill) {
          annoCtx.fillStyle = fill;
          annoCtx.fillRect(x, y, w, h);
        }
        if (dash.length) annoCtx.setLineDash(dash);
        // 深色外描边：在粉白组织上托住主色
        annoCtx.lineWidth = role === "path" ? 3 : 4;
        annoCtx.strokeStyle = halo;
        annoCtx.strokeRect(x, y, w, h);
        annoCtx.lineWidth = 2;
        annoCtx.strokeStyle = stroke;
        annoCtx.strokeRect(x, y, w, h);
        annoCtx.setLineDash([]);
        if (!animating && bb.magnification) {
          var label = "AI · " + fmtAiMag(bb.magnification);
          annoCtx.font = "600 12px -apple-system, BlinkMacSystemFont, sans-serif";
          var tw = annoCtx.measureText(label).width;
          var padX = 6, padY = 3, boxH = 18;
          var bx = x + 3, by = y + 3;
          annoCtx.fillStyle = "rgba(0, 0, 0, 0.78)";
          if (typeof annoCtx.roundRect === "function") {
            annoCtx.beginPath();
            annoCtx.roundRect(bx, by, tw + padX * 2, boxH, 4);
            annoCtx.fill();
          } else {
            annoCtx.fillRect(bx, by, tw + padX * 2, boxH);
          }
          annoCtx.fillStyle = "#FFFFFF";
          annoCtx.textBaseline = "middle";
          annoCtx.fillText(label, bx + padX, by + boxH / 2 + 0.5);
        }
        annoCtx.restore();
      });
    }
    // count_cells 计数范围（临时高亮，蓝色虚线 + 「计数范围」标签；非标注）
    if (hasCount) {
      var cb = countHighlight;
      var ctl = imgToCanvas(cb.x, cb.y);
      var cbr = imgToCanvas(cb.x + cb.w, cb.y + cb.h);
      var cx = Math.min(ctl.x, cbr.x), cy = Math.min(ctl.y, cbr.y);
      var cw = Math.abs(cbr.x - ctl.x), ch = Math.abs(cbr.y - ctl.y);
      annoCtx.save();
      annoCtx.fillStyle = COUNT_FILL;
      annoCtx.fillRect(cx, cy, cw, ch);
      annoCtx.setLineDash([6, 4]);
      annoCtx.lineWidth = 4;
      annoCtx.strokeStyle = COUNT_HALO;
      annoCtx.strokeRect(cx, cy, cw, ch);
      annoCtx.lineWidth = 2;
      annoCtx.strokeStyle = COUNT_STROKE;
      annoCtx.strokeRect(cx, cy, cw, ch);
      annoCtx.setLineDash([]);
      if (!animating) {
        var clabel = ((window.HP_I18N && window.HP_I18N.getLang()) === "en")
          ? "counted region" : "计数范围";
        annoCtx.font = "600 12px -apple-system, BlinkMacSystemFont, sans-serif";
        var ctw = annoCtx.measureText(clabel).width;
        var cbx = cx + 3, cby = cy + (ch > 26 ? ch - 21 : 3);
        annoCtx.fillStyle = "rgba(0, 40, 90, 0.8)";
        if (typeof annoCtx.roundRect === "function") {
          annoCtx.beginPath();
          annoCtx.roundRect(cbx, cby, ctw + 12, 18, 4);
          annoCtx.fill();
        } else {
          annoCtx.fillRect(cbx, cby, ctw + 12, 18);
        }
        annoCtx.fillStyle = "#FFFFFF";
        annoCtx.textBaseline = "middle";
        annoCtx.fillText(clabel, cbx + 6, cby + 9.5);
      }
      annoCtx.restore();
    }
    if (editItem && state.showAnno && canDragItem(editItem)) {
      drawEditHandles(editItem);
    }
    // 绘制中的预览
    if (state.drawMode === "arrow" && drawPreview && drawPreview.type === "arrow") {
      drawArrow(drawPreview.x1, drawPreview.y1, drawPreview.x2, drawPreview.y2, "#FFD700", t("draw.preview"));
    }
    if (state.drawMode === "freehand" && drawPreview && drawPreview.type === "freehand" && drawPreview.points.length >= 2) {
      drawFreehand(drawPreview.points, { fill: "rgba(255,215,0,0.12)", stroke: "#FFD700" }, t("draw.preview"));
    }
    // 备注气泡（在标注与手柄之上；动画/拖动期间按需精简；focus 过滤同步）
    if (state.showAnno && !animating) {
      flatAnnoItems().forEach(function (it) {
        if (state.focusAnno && it !== state.focusAnno) return; // focus 模式只显示该条气泡
        if (dragging && it !== editItem) return; // 拖动中只画选中项气泡
        var note = String(it.note || "");
        // P2-8：note 为空时不画气泡。矩形边上的短标签（drawLabel 的"标签 · 尺寸"）
        // 已展示 label+尺寸，旧实现为选中项额外生成"标签（尺寸）"气泡，内容重复。
        // 现仅在 note 非空时画气泡，且气泡只展示 note 内容本身。
        if (!note) return;
        var selected = (editItem === it);
        drawNoteBubble(it, note, selected);
      });
    }
    // §6.1：就近工具条跟随视图（平移/缩放/旋转每帧重定位；隐藏时不读布局）
    if (annoToolbarEl && !annoToolbarEl.hidden) updateAnnoToolbarPos();
  }

  // 绘制编辑手柄（管理端所有标注可编辑）
  function drawEditHandles(it) {
    var hs = editHandles(it);
    annoCtx.fillStyle = "#fff";
    annoCtx.strokeStyle = ANNO_SELECTED_STROKE;
    annoCtx.lineWidth = 2;
    hs.forEach(function (h) {
      var isMid = (h.id === "mid" || h.id === "fmid" || h.id === "move");
      if (h.vertex) {
        // polygon 顶点：实心圆点（§6.2 编辑轮廓点）
        annoCtx.beginPath();
        annoCtx.arc(h.x, h.y, 5, 0, Math.PI * 2);
        annoCtx.fill(); annoCtx.stroke();
      } else if (h.insertAfter != null) {
        // polygon 边中点「加顶点」柄：小方框
        annoCtx.globalAlpha = 0.85;
        annoCtx.fillRect(h.x - 4, h.y - 4, 8, 8);
        annoCtx.strokeRect(h.x - 4, h.y - 4, 8, 8);
        annoCtx.globalAlpha = 1;
      } else if (isMid) {
        annoCtx.beginPath();
        annoCtx.arc(h.x, h.y, 6, 0, Math.PI * 2);
        annoCtx.fill(); annoCtx.stroke();
      } else {
        annoCtx.fillRect(h.x - 5, h.y - 5, 10, 10);
        annoCtx.strokeRect(h.x - 5, h.y - 5, 10, 10);
      }
    });
  }

  // 标注几何 helper（static/anno-geometry.js；主站与分享页共用）。
  // 缺失时退化为空对象——绘制路径自行兜底（脚本加载顺序由模板保证）。
  var AG = window.HP_AnnoGeometry || {
    pointsBBox: function (pts) { return null; },
    pointSegDist: function (px, py, x1, y1, x2, y2) { return 0; },
    pointInPolygon: function () { return false; },
    polygonSelfIntersects: function () { return false; },
    translatePoints: function (pts) { return pts; },
    scalePointsAbout: function (pts, ax, ay, s) { return pts; },
    pointsShiftIntoRect: function () { return null; },
    rectQuad: function (x, y, w, h, f) { return []; },
    pointInQuad: function () { return false; },
    quadBBox: function () { return null; },
    clonePoints: function (pts) { return pts; },
  };

  // §6.1 统一选中视觉：单一蓝色轮廓 + 控制点；去掉旧「蓝外框 + 黄内框 +
  // 常驻黄角点」双描边。未选中回落各自 label 色（更轻）。
  var ANNO_SELECTED_STROKE = "#007AFF";
  var ANNO_SELECTED_FILL = "rgba(0,122,255,0.10)";

  // 矩形：四顶点经 OSD 投影成屏幕四边形（§6.2 旋转/镜像正确投影，
  // 不拿两个对角点拼屏幕轴对齐框）。返回 {quad, bbox}。
  function rectScreenQuad(it) {
    var w0 = rectItemW(it), h0 = rectItemH(it);
    var quad = AG.rectQuad(it.x, it.y, w0, h0, imgToCanvas);
    return { quad: quad, bbox: AG.quadBBox(quad) };
  }

  // 矩形描边 + 半透明填充（quad 多边形路径）；选中叠加单层蓝轮廓。
  function drawRectQuad(it, color, selected) {
    var sq = rectScreenQuad(it);
    if (!sq.quad || sq.quad.length < 3) return;
    var isAi = itemIsAi(it);
    annoCtx.save();
    annoCtx.beginPath();
    annoCtx.moveTo(sq.quad[0][0], sq.quad[0][1]);
    for (var i = 1; i < sq.quad.length; i++) annoCtx.lineTo(sq.quad[i][0], sq.quad[i][1]);
    annoCtx.closePath();
    annoCtx.fillStyle = selected ? ANNO_SELECTED_FILL
      : (isAi ? AI_ANNO_FILL : (color.fill || "rgba(0,0,0,0)"));
    annoCtx.fill();
    annoCtx.lineWidth = selected ? 3 : 2;
    annoCtx.strokeStyle = selected ? ANNO_SELECTED_STROKE
      : (isAi ? AI_ANNO_STROKE_FALLBACK : color.stroke);
    annoCtx.stroke();
    annoCtx.restore();
    return sq.bbox;
  }

  function drawAnnoItem(it, color, selected, showText) {
    var typ = it.type || "rect";
    var hlStroke = selected ? ANNO_SELECTED_STROKE : null;
    var lbl = showText ? it.label : null;
    // AI 落标（itemIsAi：带 AI 会话/溯源的标注）给半透明橙色填充，
    // 区别于人工标注（#3）
    var isAi = itemIsAi(it);
    if (typ === "rect") {
      var bbox = drawRectQuad(it, color, selected);
      if (lbl && bbox) {
        // 有备注气泡时标签改画在框内左上，避免和框顶居中的 callout 叠在一起
        var hasNote = String(it.note || "").trim();
        var sizeTxt = rectItemSizeText(it);
        drawLabel(it.label, bbox.x, bbox.y, sizeTxt, null, !!hasNote);
      }
    } else if (typ === "arrow") {
      drawArrow(it.x1, it.y1, it.x2, it.y2, hlStroke || color.stroke, lbl);
    } else if (typ === "polygon") {
      // §5.4：polygon 是独立类型（简单多边形，服务端校验非自交），
      // 画闭合多边形；绝不伪装成 freehand（类型与校验语义必须保留）。
      drawPolygon(it.points, { fill: isAi ? AI_ANNO_FILL : color.fill,
                               stroke: hlStroke || (isAi ? AI_ANNO_STROKE_FALLBACK : color.stroke) },
                  lbl);
    } else if (typ === "freehand") {
      drawFreehand(it.points, { fill: color.fill, stroke: hlStroke || color.stroke }, lbl);
    }
  }

  function drawArrow(x1, y1, x2, y2, stroke, label) {
    var a = imgToCanvas(x1, y1), b = imgToCanvas(x2, y2);
    annoCtx.lineWidth = 3;
    annoCtx.strokeStyle = stroke;
    annoCtx.fillStyle = stroke;
    annoCtx.beginPath();
    annoCtx.moveTo(a.x, a.y);
    annoCtx.lineTo(b.x, b.y);
    annoCtx.stroke();
    // 箭头三角头部（根据两端点屏幕坐标算角度）
    var ang = Math.atan2(b.y - a.y, b.x - a.x);
    var head = 12;
    annoCtx.beginPath();
    annoCtx.moveTo(b.x, b.y);
    annoCtx.lineTo(b.x - head * Math.cos(ang - Math.PI / 6), b.y - head * Math.sin(ang - Math.PI / 6));
    annoCtx.lineTo(b.x - head * Math.cos(ang + Math.PI / 6), b.y - head * Math.sin(ang + Math.PI / 6));
    annoCtx.closePath();
    annoCtx.fill();
    if (label) drawLabel(label, b.x + 6, b.y - 6, "", stroke);
  }

  function drawFreehand(points, color, label) {
    if (!points || points.length < 2) return;
    annoCtx.lineWidth = 3;
    annoCtx.strokeStyle = color.stroke;
    annoCtx.fillStyle = color.fill || "rgba(0,0,0,0.12)";
    annoCtx.beginPath();
    var p0 = imgToCanvas(points[0][0], points[0][1]);
    annoCtx.moveTo(p0.x, p0.y);
    for (var i = 1; i < points.length; i++) {
      var p = imgToCanvas(points[i][0], points[i][1]);
      annoCtx.lineTo(p.x, p.y);
    }
    annoCtx.closePath();
    annoCtx.fill();
    annoCtx.stroke();
    if (label) drawLabel(label, p0.x, p0.y, "", color.stroke);
  }

  // §5.4：polygon（简单多边形）画布呈现。与 freehand 的差别在语义（闭合
  // 简单多边形、服务端拒绝自交），绘制路径本身共用点列投影；类型不降级。
  function drawPolygon(points, color, label) {
    if (!points || points.length < 3) return;
    annoCtx.lineWidth = 3;
    annoCtx.strokeStyle = color.stroke;
    annoCtx.fillStyle = color.fill || "rgba(0,0,0,0.12)";
    annoCtx.beginPath();
    var p0 = imgToCanvas(points[0][0], points[0][1]);
    annoCtx.moveTo(p0.x, p0.y);
    for (var i = 1; i < points.length; i++) {
      var p = imgToCanvas(points[i][0], points[i][1]);
      annoCtx.lineTo(p.x, p.y);
    }
    annoCtx.closePath();
    annoCtx.fill();
    annoCtx.stroke();
    if (label) drawLabel(label, p0.x, p0.y, "", color.stroke);
  }

  // 标签文字：黄底深字（与现有 ROI 标签风格一致）
  // inside=true：画在矩形内左上（有备注气泡时用，避免和框顶 callout 重叠）
  function drawLabel(label, x, y, sizeText, strokeColor, inside) {
    var text = String(label || "");
    if (sizeText) text = (text ? text + " · " : "") + sizeText;
    if (!text) return;
    annoCtx.font = "600 11px " + "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif";
    var padX = 5, padY = 3;
    var m = annoCtx.measureText(text);
    var w = m.width + padX * 2;
    var h = 16;
    var bx = inside ? x + 3 : x;
    var by = inside ? y + 3 : y - h - 2;
    if (strokeColor && strokeColor !== "#FFD700") {
      annoCtx.fillStyle = strokeColor;
    } else {
      annoCtx.fillStyle = "#FFD700";
    }
    annoCtx.fillRect(bx, by, w, h);
    annoCtx.fillStyle = (strokeColor && strokeColor !== "#FFD700") ? "#fff" : "#5a3500";
    annoCtx.textBaseline = "middle";
    annoCtx.fillText(text, bx + padX, by + h / 2 + 0.5);
  }

  // ---------- 备注气泡（macOS callout 风格，与 share.js 一致） ----------
  var BUBBLE_FONT = "12px " + "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif";
  // 布局缓存：note 文本 → {lines, boxW, boxH}（font/maxWidth 固定，布局与视图无关，
  // 避免每帧逐字符 measureText——标注多时这是动画卡顿的主因）
  var _bubbleLayoutCache = {};
  function bubbleLayout(note) {
    var hit = _bubbleLayoutCache[note];
    if (hit) return hit;
    annoCtx.font = BUBBLE_FONT;
    var maxWidth = 240;
    var lines = wrapText(note, maxWidth);
    var padX = 8, padY = 6, lineH = 15;
    var textW = 0;
    lines.forEach(function (ln) {
      var w = annoCtx.measureText(ln).width;
      if (w > textW) textW = w;
    });
    var out = {
      lines: lines,
      boxW: Math.min(maxWidth, Math.max(20, textW)) + padX * 2,
      boxH: lines.length * lineH + padY * 2,
    };
    if (Object.keys(_bubbleLayoutCache).length > 300) _bubbleLayoutCache = {};
    _bubbleLayoutCache[note] = out;
    return out;
  }

  function wrapText(text, maxWidth) {
    annoCtx.font = BUBBLE_FONT;
    var lines = [];
    String(text).split("\n").forEach(function (para) {
      if (para === "") { lines.push(""); return; }
      var cur = "";
      for (var i = 0; i < para.length; i++) {
        var test = cur + para[i];
        if (annoCtx.measureText(test).width > maxWidth && cur) {
          lines.push(cur);
          cur = para[i];
        } else {
          cur = test;
        }
      }
      if (cur) lines.push(cur);
    });
    return lines;
  }

  function annoAnchor(it) {
    var typ = it.type || "rect";
    if (typ === "rect") {
      // §6.2 旋转/镜像下取投影四边形外接框（不是两个对角点的屏幕 AABB）
      var sq = rectScreenQuad(it);
      var bb = sq.bbox || { x: 0, y: 0, w: 0, h: 0 };
      return { x: bb.x + bb.w / 2, y: bb.y, minSide: Math.min(bb.w, bb.h) };
    } else if (typ === "arrow") {
      var a = imgToCanvas(it.x1, it.y1), b = imgToCanvas(it.x2, it.y2);
      return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, minSide: 40 };
    } else if (typ === "freehand" || typ === "polygon") {
      var pts = (it.points || []).map(function (p) { return imgToCanvas(p[0], p[1]); });
      var xs = pts.map(function (p) { return p.x; });
      var ys = pts.map(function (p) { return p.y; });
      var minx = Math.min.apply(null, xs), maxx = Math.max.apply(null, xs);
      var miny = Math.min.apply(null, ys), maxy = Math.max.apply(null, ys);
      return { x: (minx + maxx) / 2, y: miny, minSide: Math.min(maxx - minx, maxy - miny) };
    }
    return { x: 0, y: 0, minSide: 0 };
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function drawNoteBubble(it, note, selected) {
    var anchor = annoAnchor(it);
    if (anchor.minSide < 24) return;
    var c = els.annoCanvas;
    var canvasW = c.clientWidth, canvasH = c.clientHeight;

    // 布局走缓存（避免每帧逐字符 measureText）
    var layout = bubbleLayout(note);
    var lines = layout.lines;
    var boxW = layout.boxW, boxH = layout.boxH;
    var padX = 8, padY = 6, lineH = 15;

    var cx = anchor.x;
    var above = true;
    var boxX = cx - boxW / 2;
    var boxY = anchor.y - 8 - boxH;

    if (boxY < 4) { above = false; boxY = anchor.y + 10; }
    if (boxX < 4) boxX = 4;
    if (boxX + boxW > canvasW - 4) boxX = canvasW - 4 - boxW;
    if (boxY + boxH > canvasH - 4) boxY = Math.max(4, canvasH - 4 - boxH);

    var borderColor = selected ? "#007AFF" : "rgba(0,0,0,0.15)";
    var triSize = 6;
    var triTipX = cx;
    annoCtx.save();
    annoCtx.globalAlpha = 0.85;
    annoCtx.fillStyle = "#ffffff";
    roundRect(annoCtx, boxX, boxY, boxW, boxH, 8);
    annoCtx.fill();
    annoCtx.globalAlpha = 1;
    annoCtx.strokeStyle = borderColor;
    annoCtx.lineWidth = 1;
    annoCtx.stroke();
    annoCtx.restore();

    annoCtx.save();
    annoCtx.fillStyle = "#ffffff";
    annoCtx.strokeStyle = borderColor;
    annoCtx.lineWidth = 1;
    annoCtx.beginPath();
    if (above) {
      var baseY = boxY + boxH;
      annoCtx.moveTo(triTipX - triSize, baseY - 0.5);
      annoCtx.lineTo(triTipX, baseY + triSize);
      annoCtx.lineTo(triTipX + triSize, baseY - 0.5);
    } else {
      var baseY2 = boxY;
      annoCtx.moveTo(triTipX - triSize, baseY2 + 0.5);
      annoCtx.lineTo(triTipX, baseY2 - triSize);
      annoCtx.lineTo(triTipX + triSize, baseY2 + 0.5);
    }
    annoCtx.closePath();
    annoCtx.fill();
    annoCtx.stroke();
    annoCtx.restore();

    annoCtx.fillStyle = "#333";
    annoCtx.font = "12px " + "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif";
    annoCtx.textBaseline = "top";
    lines.forEach(function (ln, i) {
      annoCtx.fillText(ln, boxX + padX, boxY + padY + i * lineH);
    });
  }

  // =========================================================================
  // 编辑模式：非绘制模式下点击标注画布层，命中检测 + 选中 + 拖动手柄
  // （管理端所有标注可编辑）
  // =========================================================================
  function pointSegDist(px, py, x1, y1, x2, y2) {
    return AG.pointSegDist(px, py, x1, y1, x2, y2);
  }

  function pointInPolygon(px, py, pts) {
    return AG.pointInPolygon(px, py, pts);
  }

  function hitAnno(sx, sy) {
    var items = flatAnnoItems();
    for (var i = items.length - 1; i >= 0; i--) {
      var it = items[i];
      var typ = it.type || "rect";
      if (typ === "rect") {
        // §6.2：旋转/镜像下命中投影四边形（内部或边线 ≤6px），不再用
        // 屏幕轴对齐 AABB——旋转 45° 时旧 AABB 会命中框外空白。
        var sq = rectScreenQuad(it);
        if (sq.quad && AG.pointInQuad(sx, sy, sq.quad, 6)) return it;
      } else if (typ === "arrow") {
        var a = imgToCanvas(it.x1, it.y1), b = imgToCanvas(it.x2, it.y2);
        if (pointSegDist(sx, sy, a.x, a.y, b.x, b.y) <= 8) return it;
      } else if (typ === "polygon" || typ === "freehand") {
        // 注意：必须是 [x, y] 数组（pointInPolygon 按 pts[i][0/1] 索引；
        // 旧实现传 {x,y} 对象 → 内部命中恒假，仅边线距离可用）
        var pts = (it.points || []).map(function (p) {
          var c = imgToCanvas(p[0], p[1]);
          return [c.x, c.y];
        });
        if (pts.length >= 3 && pointInPolygon(sx, sy, pts)) return it;
        for (var k = 0; k < pts.length - 1; k++) {
          if (pointSegDist(sx, sy, pts[k].x, pts[k].y, pts[k + 1].x, pts[k + 1].y) <= 8) return it;
        }
      }
    }
    return null;
  }

  function editHandles(it) {
    var typ = it.type || "rect";
    var out = [];
    if (typ === "rect") {
      // §6.2：手柄位置 = 图像坐标角/边中点逐一投影（旋转/镜像下跟随真实
      // 顶点，不重排 AABB）。拖动语义仍是图像轴（角双轴/边单轴）。
      var w0 = rectItemW(it), h0 = rectItemH(it);
      var corners = [
        { id: "tl", ix: it.x, iy: it.y },
        { id: "t", ix: it.x + w0 / 2, iy: it.y },
        { id: "tr", ix: it.x + w0, iy: it.y },
        { id: "r", ix: it.x + w0, iy: it.y + h0 / 2 },
        { id: "br", ix: it.x + w0, iy: it.y + h0 },
        { id: "b", ix: it.x + w0 / 2, iy: it.y + h0 },
        { id: "bl", ix: it.x, iy: it.y + h0 },
        { id: "l", ix: it.x, iy: it.y + h0 / 2 },
      ];
      out = corners.map(function (c) {
        var p = imgToCanvas(c.ix, c.iy);
        return { id: c.id, x: p.x, y: p.y };
      });
    } else if (typ === "arrow") {
      var a = imgToCanvas(it.x1, it.y1), b = imgToCanvas(it.x2, it.y2);
      out = [
        { id: "p1", x: a.x, y: a.y }, { id: "p2", x: b.x, y: b.y },
        { id: "mid", x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
      ];
    } else if (typ === "polygon") {
      // §6.2：「编辑轮廓点」模式 → 拖顶点 + 边中点「加顶点」柄；
      // 普通选中 → 包围框角柄（整体移动/缩放），与 freehand 同形。
      if (state.polygonVertexEdit === it) {
        var vp = (it.points || []).map(function (p) {
          var c = imgToCanvas(p[0], p[1]);
          return { x: c.x, y: c.y };
        });
        for (var vi = 0; vi < vp.length; vi++) {
          out.push({ id: "v" + vi, x: vp[vi].x, y: vp[vi].y, vertex: true });
        }
        for (var ei = 0; ei < vp.length; ei++) {
          var e1 = vp[ei], e2 = vp[(ei + 1) % vp.length];
          out.push({ id: "n" + ei, x: (e1.x + e2.x) / 2, y: (e1.y + e2.y) / 2,
                     insertAfter: ei });
        }
        return out;
      }
      var pb = AG.pointsBBox(it.points || []);
      if (!pb) return out;
      var ptl = imgToCanvas(pb.x, pb.y), pbr = imgToCanvas(pb.x + pb.w, pb.y + pb.h);
      var px = Math.min(ptl.x, pbr.x), py = Math.min(ptl.y, pbr.y);
      var pw = Math.abs(pbr.x - ptl.x), ph = Math.abs(pbr.y - ptl.y);
      out = [
        { id: "ftl", x: px, y: py }, { id: "ftr", x: px + pw, y: py },
        { id: "fbr", x: px + pw, y: py + ph }, { id: "fbl", x: px, y: py + ph },
        { id: "fmid", x: px + pw / 2, y: py + ph / 2 },
      ];
    } else if (typ === "freehand") {
      var xs = it.points.map(function (p) { return p[0]; });
      var ys = it.points.map(function (p) { return p[1]; });
      var minx = Math.min.apply(null, xs), miny = Math.min.apply(null, ys);
      var maxx = Math.max.apply(null, xs), maxy = Math.max.apply(null, ys);
      var tl2 = imgToCanvas(minx, miny), br2 = imgToCanvas(maxx, maxy);
      var x2 = Math.min(tl2.x, br2.x), y2 = Math.min(tl2.y, br2.y);
      var w2 = Math.abs(br2.x - tl2.x), h2 = Math.abs(br2.y - tl2.y);
      out = [
        { id: "ftl", x: x2, y: y2 }, { id: "ftr", x: x2 + w2, y: y2 },
        { id: "fbr", x: x2 + w2, y: y2 + h2 }, { id: "fbl", x: x2, y: y2 + h2 },
        { id: "fmid", x: x2 + w2 / 2, y: y2 + h2 / 2 },
      ];
    }
    return out;
  }

  // 手柄命中容差（CSS px）：视觉柄可小，命中范围桌面 ≈16px、触摸 ≈28px（§6.1）。
  function handleHitTolerance() {
    var coarse = false;
    try {
      coarse = !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
    } catch (e) {}
    return coarse ? 14 : 8;
  }

  function hitHandle(sx, sy, it) {
    var hs = editHandles(it);
    var tol = handleHitTolerance();
    for (var i = 0; i < hs.length; i++) {
      if (Math.hypot(sx - hs[i].x, sy - hs[i].y) <= tol) return hs[i];
    }
    return null;
  }

  // 工单 D：选中即快照（服务端态）——后续编辑（拖柄/改备注）的「上一版」，
  // commitAdminEdit 成功后作为撤销依据。重新选中会刷新快照。
  var editBeforeSnapshot = null;

  // ---- 位置锁 / 可编辑判定（§6.3；后端契约：条目顶层 geometry_locked） ----
  function itemGeometryLocked(it) {
    return !!(it && (it.geometry_locked === true || it.geometry_locked === "true"));
  }
  // 服务端能力位优先（can_edit=false 只读）；缺省按现有工作台语义可编辑。
  function itemCanEdit(it) {
    if (!it) return false;
    if (it.can_edit === false || it.can_edit === "false") return false;
    return true;
  }
  // 只读 = 无编辑权（无手柄、不可拖）；锁定 = 位置冻结但仍可改备注/解锁。
  function itemIsReadOnly(it) {
    return !itemCanEdit(it);
  }
  // AI 判定以服务端 author_kind 为准（与导出 ai_generated 同口径，只有带
  // AI 会话或溯源的标注才是 AI）；2026-10-19 前工作台人工标注在库里存为
  // source=ai，无会话/溯源，其 author_kind 为 user。缺 author_kind
  // （旧接口形态/本地构造的条目）回落 source。
  function itemIsAi(it) {
    if (!it) return false;
    if (it.author_kind) return it.author_kind === "ai";
    return it.source === "ai";
  }
  function itemIsAiPending(it) {
    return !!(itemIsAi(it) && it.review_status === "pending");
  }

  function selectEditItem(it) {
    editItem = it;
    // §6.1：选中不再隐藏其它标记（「只看这条」收进 ⋯ 菜单，与选择分开）
    editing = false;  // 备注卡回到查看形态（柄显示只看 canDragItem，§6.2）
    state.polygonVertexEdit = null;
    editBeforeSnapshot = {
      token: it.token,
      annotationId: it.annotation_id || null,
      index: it.index != null ? it.index : null,
      geom: snapshotGeom(it),
      note: it.note != null ? it.note : "",
      revision: Number(it.revision) > 0 ? Number(it.revision) : 0,
      geometryLocked: itemGeometryLocked(it),
    };
    redrawAnnoCanvas();
    openAnnoToolbar();
    openEditCard(it);
  }

  function clearEditItem() {
    editItem = null;
    editing = false;
    state.polygonVertexEdit = null;
    editBeforeSnapshot = null;
    closeAnnoToolbar();
    closeEditCard();
    redrawAnnoCanvas();
  }

  // ---------- 显示全部标记（切换画布层显隐） ----------
  // 同步所有相关按钮的 active 态：旧 #anno-all-btn + 新面板头部 #anno-all-toggle
  function syncAnnoAllBtns() {
    if (els.annoAllBtn) els.annoAllBtn.classList.toggle("active", state.showAnno);
    if (els.annoAllToggle) {
      els.annoAllToggle.classList.toggle("active", state.showAnno);
      els.annoAllToggle.setAttribute("aria-pressed", state.showAnno ? "true" : "false");
    }
  }
  function toggleAnnoAll() {
    // 👁 =「显示全部标记」语义：若当前处于"只看选中那条"的 focus 状态，
    // 先清空 focus 恢复显示全部；否则在「显示全部 ↔ 全部隐藏」之间切换。
    // （画布层非绘制时 pointer-events:none，无法点空白取消 focus，故由该钮兜底。）
    if (state.focusAnno) {
      state.focusAnno = null;
      state.showAnno = true;
    } else {
      state.showAnno = !state.showAnno;
    }
    syncAnnoAllBtns();
    redrawAnnoCanvas();
  }
  // 旧函数别名（兼容）
  function clearAnnoOverlays() { annoOverlays = []; redrawAnnoCanvas(); }
  function refreshAnnoOverlays() { redrawAnnoCanvas(); }

  // =========================================================================
  // 标注绘制工具（arrow / freehand）
  // =========================================================================
  var drawPreview = null;     // {type, ...}
  var drawPointer = null;     // 当前指针捕获信息

  // =========================================================================
  // 工单 D（§5）：绘制会话状态机 + 可重试草稿 + 幂等提交
  //   idle → drawing → saving →（成功）selected（选中新标注/回到移动工具）
  //   saving 失败 → idle + retryDraft（几何保留、显示未保存、可 Enter 重试）
  // =========================================================================
  var drawPhase = "idle";     // "idle" | "drawing" | "saving"
  var retryDraft = null;      // 保存失败后的可重试草稿 {kind, geom, clientActionId}
  var drawUnsaved = false;    // 「未保存」提示（title + 状态标记）

  function setDrawPhase(p) {
    drawPhase = p;
    if (p === "idle" && !retryDraft) setDrawUnsaved(false);
    updateResearchBusy();  // P3：绘制/保存中取消稳定观察检测
  }

  // 未保存提示：不动布局（工单 D 约束），只在「保存标记」按钮 title/状态上
  // 标记 + 由失败 toast 告知。
  function setDrawUnsaved(on) {
    drawUnsaved = !!on;
    var btn = els.saveAnnoBtn;
    if (!btn) return;
    if (on) {
      btn.title = t("draw.unsaved.tip");
      btn.setAttribute("data-unsaved", "1");
    } else {
      // 防御：部分最小 DOM 桩没有 removeAttribute（真实浏览器恒有）
      if (typeof btn.removeAttribute === "function") {
        try { btn.removeAttribute("data-unsaved"); } catch (err) {}
      } else {
        btn.setAttribute("data-unsaved", "0");
      }
      var key = btn.getAttribute("data-i18n-title");
      btn.title = key ? t(key) : "";
    }
  }

  function newClientActionId() {
    return "web-" + Date.now().toString(36) + "-" +
      Math.random().toString(36).slice(2, 10);
  }

  // 从当前 UI 态收集「有效未提交草稿」：失败重试草稿优先，其次矩形选区，
  // 最后箭头/描图预览。无草稿返回 null。
  function collectPendingDraft() {
    if (retryDraft) return retryDraft;
    if (rectToolActive() && state.roi.w > 0) {
      return {
        kind: "rect",
        geom: { x: Math.round(state.roi.x), y: Math.round(state.roi.y),
                w: Math.round(state.roi.w), h: Math.round(state.roi.h) },
        clientActionId: null,
      };
    }
    if (state.drawMode && drawPreview) {
      var g = validPreviewGeom(drawPreview);
      if (g) {
        return { kind: drawPreview.type, geom: g, clientActionId: null };
      }
    }
    return null;
  }

  // 预览几何的有效性判定（与 finishDraw 的取消阈值同口径）：
  // 有效返回可提交 geom，无效返回 null。
  function validPreviewGeom(dp) {
    if (!dp) return null;
    if (dp.type === "arrow") {
      if (Math.hypot(dp.x2 - dp.x1, dp.y2 - dp.y1) < 10) return null;
      return { x1: dp.x1, y1: dp.y1, x2: dp.x2, y2: dp.y2 };
    }
    if (dp.type === "freehand") {
      var pts = dp.points || [];
      if (pts.length < 3) return null;
      var xs = pts.map(function (p) { return p[0]; });
      var ys = pts.map(function (p) { return p[1]; });
      var bb = Math.max(Math.max.apply(null, xs) - Math.min.apply(null, xs),
                        Math.max.apply(null, ys) - Math.min.apply(null, ys));
      if (bb < 10) return null;
      return { points: pts.map(function (p) { return [p[0], p[1]]; }) };
    }
    return null;
  }

  // Enter / 拖动完成 / 「保存标记」的统一入口：只提交有效未提交草稿
  function submitCurrentDraft() {
    var draft = collectPendingDraft();
    if (!draft) return;
    submitAnnotationDraft(draft);
  }

  // 保存失败后把草稿恢复成可见预览（箭头/描图），几何不清空
  function restorePreviewFromDraft(draft) {
    if (!draft || draft.kind === "rect") return;
    if (draft.kind === "arrow") {
      drawPreview = { type: "arrow", x1: draft.geom.x1, y1: draft.geom.y1,
                      x2: draft.geom.x2, y2: draft.geom.y2, armed: false };
    } else if (draft.kind === "freehand") {
      drawPreview = { type: "freehand",
                      points: draft.geom.points.map(function (p) { return [p[0], p[1]]; }),
                      lastScreen: null };
    }
    redrawAnnoCanvas();
  }

  // 统一提交（工单 D）：冻结 slide/几何/身份/幂等键；保存中忽略重复提交；
  // 切片切换后的回包不落到新切片；失败保留可重试草稿（不退工具、不清几何）。
  function submitAnnotationDraft(draft) {
    if (!state.slide) return;
    if (drawPhase === "saving") return; // 保存中禁止重复提交
    var slideName = state.slide.name;   // 冻结提交时的切片（名称快照）
    var slideId = state.slide.id || null;  // 冻结提交时的 slide_id（P2 操作键）
    var label = (els.annoLabelInput.value || "").trim();
    if (!label) label = t("anno.default.user");
    // P2 合同 §3.1：slide_id（权威）+ slide（名称快照）双字段并存；同名不同
    // ID 资产由服务端按 slide_id 裁决（两字段冲突会 400，前端不做猜测）
    var body = { slide: slideName, type: draft.kind, label: label, shared: false, note: "" };
    if (slideId) body.slide_id = slideId;
    for (var k in draft.geom) body[k] = draft.geom[k];
    // §6.3：临时框草稿锁随保存落库（POST 可选 geometry_locked；不因刷新解锁）
    if (draft.kind === "rect" && state.roiGeometryLocked) {
      body.geometry_locked = true;
    }
    // 幂等键：重试沿用同一键（双击/重试不产生第二条）
    body.client_action_id = draft.clientActionId ||
      (retryDraft && retryDraft.clientActionId) || newClientActionId();
    var actionId = body.client_action_id;
    setDrawPhase("saving");
    if (els.saveAnnoBtn) els.saveAnnoBtn.disabled = true;
    apiFetch("/api/annotation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) {
          throw new Error(j.error || t("save.fail"));
        });
        return r.json();
      })
      .then(function (j) {
        // 切片已切换：回包不污染新切片（草稿静默丢弃，不套用/不选中；
        // P2：按 slide_id 比对，旧后端/无 id 回落名）
        var slideSwitched = !state.slide || (slideId
          ? String(state.slide.id) !== String(slideId)
          : state.slide.name !== slideName);
        if (slideSwitched) {
          retryDraft = null;
          drawPreview = null;
          drawPointer = null;
          setDrawPhase("idle");
          return;
        }
        retryDraft = null;
        setDrawUnsaved(false);
        // P3 研究采集：业务写入成功才记 annotation_create（失败路径在 catch，
        // 不产事件；§7.1）。业务标注 id 由采集模块换成会话内匿名局部 ID。
        if (researchTelemetry) {
          researchTelemetry.notifyAnnotation({
            action: "annotation_create",
            shapeType: draft.kind,
            geom: draft.geom,
            annotationId: j && j.annotation_id,
          });
        }
        // 撤销单元 = 语义操作（这次创建），压入撤销栈（限定本身份/本切片；
        // P2：切片作用域按 slide_id，旧数据/旧后端回落名）
        var entry = pushUndoEntry({
          kind: "create",
          slide: slideName,
          slide_id: slideId,
          userId: currentUserId,
          annoKind: draft.kind,
          geom: JSON.parse(JSON.stringify(draft.geom)),
          label: label,
          annotationId: j && j.annotation_id,
          index: j && j.index,
          revision: j && j.revision,
          clientActionId: actionId,
        });
        if (draft.kind === "rect") {
          toast(t("anno.saved.tip"), "success");
        } else {
          toast(t("anno.saved"), "success");
        }
        // 成功后回到移动/平移工具；刷新并按 annotation_id 选中新标注
        if (draft.kind === "rect") { exitRoi(); } else { exitDrawMode(); }
        refreshCurrentAnnotations().then(function () {
          selectCreatedAnnotation(j && j.annotation_id);
        });
        loadAnnotationsIndex().then(function () {
          renderFolderBrowser();
        });
        // 保存期间用户按过撤销 → 现在执行该次创建的逆操作（仍是权限内 DELETE）
        if (pendingUndoAfterSave && entry) {
          pendingUndoAfterSave = false;
          performUndo();
        }
      })
      .catch(function (e) {
        // 失败：可重试草稿；不清几何、不退工具（工单 D 核心契约）。
        // 保存未成功 → 之前记录的撤销意图作废（没有创建就无逆操作）。
        pendingUndoAfterSave = false;
        retryDraft = {
          kind: draft.kind,
          geom: draft.geom,
          clientActionId: actionId,
        };
        setDrawPhase("idle");
        setDrawUnsaved(true);
        if (draft.kind === "rect") {
          if (els.saveAnnoBtn) els.saveAnnoBtn.disabled = false; // 允许按钮重试
          if (els.saveBtn) els.saveBtn.disabled = false;         // 裁剪导出不受保存失败影响
        } else {
          restorePreviewFromDraft(retryDraft);
        }
        toast(t("draw.unsaved.retry", { e: e.message }), "error");
      });
  }

  function enterDrawMode(mode) {
    if (!state.slide) { toast(t("roi.need.slide"), "error"); return; }
    exitRoi();
    state.drawMode = mode;
    els.annoArrowBtn.classList.toggle("active", mode === "arrow");
    els.annoFreeBtn.classList.toggle("active", mode === "freehand");
    var c = els.annoCanvas;
    c.classList.add("drawing");
    if (viewer) viewer.setMouseNavEnabled(false);
    syncAnnoAllBtns();
    redrawAnnoCanvas();
    updateCtxBar();
    // §3.3 narrow 档「只留当前工具」：激活的绘制工具所在组保留在主行
    applyToolbarTier();
    toast(mode === "arrow" ? t("draw.arrow.tip") : t("draw.free.tip"), "info");
    updateResearchBusy();  // P3
  }

  function exitDrawMode() {
    state.drawMode = null;
    drawPreview = null;
    drawPointer = null;
    retryDraft = null;           // 工单 D：退出工具 = 放弃未保存草稿
    setDrawUnsaved(false);
    setDrawPhase("idle");
    els.annoArrowBtn.classList.remove("active");
    els.annoFreeBtn.classList.remove("active");
    if (els.annoCanvas) els.annoCanvas.classList.remove("drawing");
    if (viewer) viewer.setMouseNavEnabled(true);
    redrawAnnoCanvas();
    updateCtxBar();
    applyToolbarTier();
    updateResearchBusy();  // P3
  }

  function toggleDrawMode(mode) {
    if (state.drawMode === mode) { exitDrawMode(); return; }
    enterDrawMode(mode);
  }

  function onAnnoPointerDown(e) {
    if (!state.slide) return;
    // 工单 E：右键（button=2）不进入绘制/编辑/选中路径——右键归上下文
    // 菜单（contextmenu 事件统一处理）；绘制中的右键取消逻辑归工单 D。
    if (e && e.button === 2) return;
    // 矩形工具优先（升级 C：画布层拖出矩形/点击中心放置）
    if (rectToolActive()) {
      onRectCanvasPointerDown(e);
      return;
    }
    // 绘制模式优先
    if (state.drawMode) {
      if (e.button === 2) return; // 右键不启动绘制（留给右键菜单）
      e.preventDefault(); e.stopPropagation();
      // 重新落笔 = 放弃失败草稿的重试（Enter 重试在此之前仍可用）
      if (retryDraft) { retryDraft = null; setDrawUnsaved(false); }
      var c = els.annoCanvas;
      var img0 = screenToImg(e);
      if (state.drawMode === "arrow") {
        // 箭头（工单 D）：支持两种完成方式——
        //  a) 按下拖到终点松开（原有）；
        //  b) 单击起点 → 移动预览 → 再单击/双击/Enter 定终点。
        // 已有 armed 起点 → 本次按下即定终点（松开时判定）。
        if (drawPreview && drawPreview.type === "arrow" && drawPreview.armed) {
          try { c.setPointerCapture(e.pointerId); } catch (err) {}
          drawPointer = { id: e.pointerId };
          drawPreview.armed = false;
          drawPreview.pressing = true;
          drawPreview.x2 = img0.x; drawPreview.y2 = img0.y;
          drawPreview.sx0 = e.clientX; drawPreview.sy0 = e.clientY;
        } else {
          try { c.setPointerCapture(e.pointerId); } catch (err) {}
          drawPointer = { id: e.pointerId };
          drawPreview = { type: "arrow", x1: img0.x, y1: img0.y, x2: img0.x, y2: img0.y,
                          armed: true, pressing: true,
                          sx0: e.clientX, sy0: e.clientY };
        }
        setDrawPhase("drawing");
      } else {
        try { c.setPointerCapture(e.pointerId); } catch (err) {}
        drawPointer = { id: e.pointerId };
        drawPreview = { type: "freehand", points: [[img0.x, img0.y]],
                        lastScreen: screenPt(e),
                        sx0: e.clientX, sy0: e.clientY, moved: false };
        setDrawPhase("drawing");
      }
      redrawAnnoCanvas();
      return;
    }
    // 非绘制模式：编辑/选中（本画布路径只在 drawing 态可达；普通对象编辑
    // 走 viewer 容器捕获路由 routeObjectPointerDown，§6.2）
    if (!state.showAnno) return;
    e.preventDefault(); e.stopPropagation();
    var sp = screenPt(e);
    // 点中手柄 → 拖动手柄（§6.2：选中即可拖柄；锁定/只读无柄）
    if (editItem && canDragItem(editItem)) {
      var hd = hitHandle(sp.x, sp.y, editItem);
      if (hd) {
        startEditDrag(e, editItem, hd);
        return;
      }
    }
    // 命中标注 → 重新选中查看（备注卡复位；选中即可拖柄/移动，§6.2）
    var hit = hitAnno(sp.x, sp.y);
    if (hit) {
      selectEditItem(hit);
      return;
    }
    // 点空白 → 取消选中
    clearEditItem();
  }

  function onAnnoPointerMove(e) {
    // 矩形工具：拖出预览（升级 C）
    if (rectToolActive() && rectDrawInfo) {
      onRectCanvasPointerMove(e);
      return;
    }
    if (state.drawMode && drawPreview) {
      e.preventDefault(); e.stopPropagation();
      var img = screenToImg(e);
      if (drawPreview.type === "arrow") {
        // 拖动中或 armed 悬停都更新终点（armed 时无指针捕获也可预览）
        drawPreview.x2 = img.x; drawPreview.y2 = img.y;
      } else if (drawPointer) {
        // 描图只在按住时收集点（悬停不加点；恢复的失败草稿不被鼠标漂移污染）
        var sp0 = screenPt(e);
        if (Math.hypot(e.clientX - drawPreview.sx0, e.clientY - drawPreview.sy0)
            > RECT_DRAG_SCREEN_PX) {
          drawPreview.moved = true;
        }
        var last = drawPreview.lastScreen;
        if (!last || Math.hypot(sp0.x - last.x, sp0.y - last.y) > 4) {
          drawPreview.points.push([img.x, img.y]);
          drawPreview.lastScreen = sp0;
          if (drawPreview.points.length >= 500) { finishDraw(); return; }
        }
      }
      redrawAnnoCanvas();
      return;
    }
    if (!editDrag) return;
    e.preventDefault(); e.stopPropagation();
    applyEditDrag(e);
  }

  function onAnnoPointerUp(e) {
    // 矩形工具：完成拖出 / 点击放置（升级 C）
    if (rectToolActive() && rectDrawInfo) {
      onRectCanvasPointerUp(e);
      return;
    }
    if (state.drawMode && drawPreview) {
      e.preventDefault(); e.stopPropagation();
      var c = els.annoCanvas;
      if (drawPointer) { try { c.releasePointerCapture(drawPointer.id); } catch (err) {} }
      drawPointer = null;
      var dragged = Math.hypot(e.clientX - drawPreview.sx0,
                               e.clientY - drawPreview.sy0) > RECT_DRAG_SCREEN_PX;
      if (drawPreview.type === "arrow") {
        if (drawPreview.armed && !dragged) {
          // 首次单击：只定起点，不产生零长度记录（工单 D）
          drawPreview.pressing = false;
          setDrawPhase("idle");
          redrawAnnoCanvas();
          return;
        }
        // 拖动松开（原有路径）或第二击定终点 → 完成
        finishDraw();
        return;
      }
      // 描图：拖动松开完成；纯单击（无位移）静默清稿留在工具内
      if (!drawPreview.moved && drawPreview.points.length < 3) {
        drawPreview = null;
        setDrawPhase("idle");
        redrawAnnoCanvas();
        return;
      }
      finishDraw();
      return;
    }
    if (!editDrag) return;
    e.preventDefault(); e.stopPropagation();
    endEditDrag(e);
  }

  // 工单 D：pointercancel / 丢失指针捕获 = 取消恢复，绝不走完成/保存路径。
  // - 矩形：恢复拖前选区（onRectCanvasPointerCancel）
  // - 箭头：armed 起点保留（等待终点），活动笔画丢弃
  // - 描图：丢弃当前笔画，留在工具内
  function onAnnoPointerCancel(e) {
    if (rectToolActive() && rectDrawInfo) {
      onRectCanvasPointerCancel(e);
      return;
    }
    if (state.drawMode && drawPreview) {
      if (e && e.preventDefault) { try { e.preventDefault(); } catch (err) {} }
      var c = els.annoCanvas;
      if (drawPointer) { try { c.releasePointerCapture(drawPointer.id); } catch (err) {} }
      drawPointer = null;
      if (drawPreview.type === "arrow" && drawPreview.armed) {
        drawPreview.pressing = false; // 起点仍在，等待终点
      } else {
        drawPreview = null;           // 丢弃被中断的笔画
      }
      setDrawPhase("idle");
      redrawAnnoCanvas();
      return;
    }
    if (editDrag) { cancelEditDragRestore(); }
  }

  // ---------- 编辑拖动会话（§6.2：一次 pointer 会话只交给一个处理器） ----------
  // el：捕获指针的元素（viewer 容器；绘制画布路径仍用 anno-canvas）。
  // handle：editHandles() 的柄对象（含 id 与可选 insertAfter/vertex）。
  function startEditDrag(e, it, handle, el) {
    var c = el || els.annoCanvas;
    try { c.setPointerCapture(e.pointerId); } catch (err) {}
    editDrag = {
      pointerId: e.pointerId,
      captureEl: c,
      handle: handle && typeof handle === "object" ? handle.id : handle,
      insertAfter: handle && typeof handle === "object" ? handle.insertAfter : null,
      item: it,
      start: snapshotGeom(it),
      startImg: screenToImg(e),
    };
    if (viewer) viewer.setMouseNavEnabled(false);
  }

  function snapshotGeom(it) {
    var typ = it.type || "rect";
    if (typ === "rect") {
      return { x: it.x, y: it.y, w: rectItemW(it), h: rectItemH(it),
               side_px: it.side_px };
    }
    if (typ === "arrow") return { x1: it.x1, y1: it.y1, x2: it.x2, y2: it.y2 };
    if (typ === "freehand" || typ === "polygon") {
      return { points: (it.points || []).map(function (p) { return [p[0], p[1]]; }) };
    }
    return {};
  }

  function applyEditDrag(e) {
    var d = editDrag;
    var it = d.item;
    var typ = it.type || "rect";
    var cur = screenToImg(e);
    var dx = cur.x - d.startImg.x;
    var dy = cur.y - d.startImg.y;
    var s = d.start;

    if (typ === "rect") {
      // 升级 C（§6.1）：四角改双轴、四边改单轴——不再 max(w,h) 正方形化。
      var W = state.slide.width, H = state.slide.height;
      if (d.handle === "move") {
        it.x = clamp(Math.round(s.x + dx), 0, Math.max(0, W - s.w));
        it.y = clamp(Math.round(s.y + dy), 0, Math.max(0, H - s.h));
      } else {
        var moveL = d.handle.indexOf("l") >= 0;
        var moveR = d.handle.indexOf("r") >= 0;
        var moveT = d.handle.indexOf("t") >= 0;
        var moveB = d.handle.indexOf("b") >= 0;
        var anchorX = moveL ? s.x + s.w : (moveR ? s.x : s.x);
        if (!moveL && !moveR) anchorX = s.x; // t/b 不改 x 轴
        var anchorY = moveT ? s.y + s.h : (moveB ? s.y : s.y);
        if (!moveT && !moveB) anchorY = s.y;
        var x0 = (moveL || moveR) ? anchorX : s.x;
        var x1 = (moveL || moveR) ? cur.x : s.x + s.w;
        var y0 = (moveT || moveB) ? anchorY : s.y;
        var y1 = (moveT || moveB) ? cur.y : s.y + s.h;
        var n = normalizeRect(x0, y0, x1, y1);
        if (n) {
          it.x = n.x; it.y = n.y; it.w = n.w; it.h = n.h;
        }
      }
    } else if (typ === "arrow") {
      if (d.handle === "p1") {
        it.x1 = Math.max(0, Math.round(s.x1 + dx));
        it.y1 = Math.max(0, Math.round(s.y1 + dy));
      } else if (d.handle === "p2") {
        it.x2 = Math.max(0, Math.round(s.x2 + dx));
        it.y2 = Math.max(0, Math.round(s.y2 + dy));
      } else if (d.handle === "mid") {
        it.x1 = Math.max(0, Math.round(s.x1 + dx));
        it.y1 = Math.max(0, Math.round(s.y1 + dy));
        it.x2 = Math.max(0, Math.round(s.x2 + dx));
        it.y2 = Math.max(0, Math.round(s.y2 + dy));
      }
    } else if (typ === "polygon" || typ === "freehand") {
      if (d.handle === "fmid" || d.handle === "move") {
        // 整体移动（§6.2：freehand/polygon 整体移动；越界整体收回边界）
        var moved = AG.translatePoints(s.points, Math.round(dx), Math.round(dy));
        var shift1 = AG.pointsShiftIntoRect(moved, state.slide.width, state.slide.height);
        if (shift1) moved = AG.translatePoints(moved, shift1.dx, shift1.dy);
        it.points = moved;
      } else if (d.handle && d.handle.charAt(0) === "v") {
        // polygon 编辑轮廓点：拖单个顶点（§6.2「编辑轮廓点」）
        var vi = parseInt(d.handle.slice(1), 10) || 0;
        var vpts = AG.clonePoints(s.points);
        if (vi < vpts.length) {
          vpts[vi] = [Math.max(0, Math.round(s.points[vi][0] + dx)),
                      Math.max(0, Math.round(s.points[vi][1] + dy))];
          it.points = vpts;
        }
      } else if (d.handle && d.handle.charAt(0) === "n" && d.insertAfter != null) {
        // polygon 加顶点：把新顶点插到 insertAfter 之后并随指针拖动
        // （dragPoints 为本次会话的工作副本，不污染 start 快照——
        //   endEditDrag 仍以 start 判定「有无有效位移」）
        if (!d.inserted) {
          var base = AG.clonePoints(d.dragPoints || s.points);
          base.splice(d.insertAfter + 1, 0, [Math.round(cur.x), Math.round(cur.y)]);
          d.inserted = true;
          d.dragPoints = base;
        }
        var npts = AG.clonePoints(d.dragPoints);
        var nidx = d.insertAfter + 1;
        npts[nidx] = [Math.max(0, Math.round(cur.x)), Math.max(0, Math.round(cur.y))];
        d.dragPoints = npts;
        it.points = npts;
      } else {
        // 包围框缩放（等比，锚对角）——freehand 与 polygon 同形
        var pts = s.points;
        var xs0 = pts.map(function (p) { return p[0]; });
        var ys0 = pts.map(function (p) { return p[1]; });
        var minx0 = Math.min.apply(null, xs0), maxx0 = Math.max.apply(null, xs0);
        var miny0 = Math.min.apply(null, ys0), maxy0 = Math.max.apply(null, ys0);
        var w0 = Math.max(1, maxx0 - minx0), h0 = Math.max(1, maxy0 - miny0);
        var aX = (d.handle === "ftl") ? maxx0 : minx0;
        var aY = (d.handle === "ftl") ? maxy0 : miny0;
        if (d.handle === "ftr") { aX = minx0; aY = maxy0; }
        if (d.handle === "fbr") { aX = minx0; aY = miny0; }
        if (d.handle === "fbl") { aX = maxx0; aY = miny0; }
        var newW = Math.max(2, Math.abs(cur.x - aX));
        var newH = Math.max(2, Math.abs(cur.y - aY));
        var scale = Math.max(newW / w0, newH / h0);
        var newPts = AG.scalePointsAbout(pts, aX, aY, scale).map(function (p) {
          return [Math.round(p[0]), Math.round(p[1])];
        });
        var shift2 = AG.pointsShiftIntoRect(newPts, state.slide.width, state.slide.height);
        if (shift2) newPts = AG.translatePoints(newPts, shift2.dx, shift2.dy);
        it.points = newPts;
      }
    }
    redrawAnnoCanvas();
  }

  function endEditDrag(e) {
    var d = editDrag;
    var c = d ? (d.captureEl || els.annoCanvas) : els.annoCanvas;
    if (d) {
      try { c.releasePointerCapture(d.pointerId); } catch (err) {}
    }
    editDrag = null;
    if (viewer) viewer.setMouseNavEnabled(true);
    // §6.4：有效拖动松手自动保存——一次 PATCH（差量），无有效位移不发。
    if (d && d.item) {
      saveItemGeometryIfChanged(d.item, d.start);
      // polygon 编辑轮廓点：拖动已保存 → 基线更新，「完成」时不再重复 PATCH
      if (state.polygonVertexEdit === d.item) {
        d.item.__vertexEditStart = snapshotGeom(d.item);
      }
    }
  }

  // 工单 D：编辑拖拽被打断 → 恢复拖前几何（本地快照），不保留半截修改
  function cancelEditDragRestore() {
    var d = editDrag;
    var c = d ? (d.captureEl || els.annoCanvas) : els.annoCanvas;
    if (d) { try { c.releasePointerCapture(d.pointerId); } catch (err) {} }
    editDrag = null;
    if (d && d.item && d.start) {
      var s = d.start, it = d.item;
      var typ = it.type || "rect";
      if (typ === "rect") {
        it.x = s.x; it.y = s.y; it.w = s.w; it.h = s.h;
        if (s.side_px != null) it.side_px = s.side_px;
      } else if (typ === "arrow") {
        it.x1 = s.x1; it.y1 = s.y1; it.x2 = s.x2; it.y2 = s.y2;
      } else if (typ === "freehand" || typ === "polygon") {
        it.points = s.points;
      }
    }
    if (viewer) viewer.setMouseNavEnabled(true);
    redrawAnnoCanvas();
    updateAnnoToolbarPos();
  }

  // 完成绘制（工单 D）：几何无效 → 只撤当前草稿（留在工具内，QuPath 习惯）；
  // 有效 → submitAnnotationDraft 自动保存。失败路径由 submitAnnotationDraft
  // 保留可重试草稿，不在这里退工具。
  function finishDraw() {
    var dp = drawPreview;
    var c = els.annoCanvas;
    if (drawPointer) { try { c.releasePointerCapture(drawPointer.id); } catch (err) {} }
    drawPointer = null;
    drawPreview = null;
    if (!dp) { exitDrawMode(); return; }
    var g = validPreviewGeom(dp);
    if (!g) {
      if (dp.type === "arrow") { toast(t("draw.short.cancel"), "info"); }
      else if ((dp.points || []).length < 3) { toast(t("draw.few.cancel"), "info"); }
      else { toast(t("draw.small.cancel"), "info"); }
      setDrawPhase("idle");
      redrawAnnoCanvas();
      return;
    }
    submitAnnotationDraft({ kind: dp.type, geom: g, clientActionId: null });
  }

  // 撤销当前绘制草稿（不清别人的东西）：矩形走 discardRectDraft；
  // 描图先撤最后一个控制点，点数不足再清整条；箭头直接清。
  function undoWhileDrawing() {
    if (rectDrawInfo) { onRectCanvasPointerCancel(null); return true; }
    if (rectToolActive()) {
      if (retryDraft && retryDraft.kind === "rect") { discardRectDraft(); return true; }
      if (state.roi.w > 0) { discardRectDraft(); return true; }
      return false;
    }
    if (state.drawMode && drawPreview) {
      if (drawPreview.type === "freehand" && drawPreview.points.length > 1) {
        drawPreview.points.pop();
        if (!drawPreview.lastScreen) drawPreview.lastScreen = null;
        redrawAnnoCanvas();
        return true;
      }
      drawPreview = null;
      drawPointer = null;
      setDrawPhase("idle");
      redrawAnnoCanvas();
      return true;
    }
    if (state.drawMode && retryDraft) {
      // 失败草稿：撤草稿、留在工具内
      retryDraft = null;
      setDrawUnsaved(false);
      drawPreview = null;
      redrawAnnoCanvas();
      return true;
    }
    return false;
  }

  // 屏幕坐标 → 图像坐标
  function screenToImg(e) {
    var rect = viewer.container.getBoundingClientRect();
    var p = viewer.viewport.viewerElementToImageCoordinates(
      new OpenSeadragon.Point(e.clientX - rect.left, e.clientY - rect.top));
    return { x: Math.round(p.x), y: Math.round(p.y) };
  }
  function screenPt(e) {
    var rect = viewer.container.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  // 保存管理员标注（arrow/freehand/rect 统一入口；工单 D 后为幂等提交包装）
  function saveAnnotation(geom) {
    if (!state.slide) return;
    if (!geom || !geom.type) return;
    var g = {};
    for (var k in geom) {
      if (k === "type") continue;
      g[k] = geom[k];
    }
    submitAnnotationDraft({ kind: geom.type, geom: g, clientActionId: null });
  }

  // 创建成功后按 annotation_id 选中新标注（0056 响应字段；A 工单已加）
  function selectCreatedAnnotation(annotationId) {
    if (!annotationId) return;
    var item = findItemByAnnotationId(annotationId);
    if (!item) return;
    // 选中即显示：打开 showAnno 以便新标注可见（focus 只显示该条）
    if (!state.showAnno) {
      state.showAnno = true;
      syncAnnoAllBtns();
    }
    selectEditItem(item);
  }

  function findItemByAnnotationId(annotationId) {
    if (!annotationId) return null;
    var items = flatAnnoItems();
    for (var i = 0; i < items.length; i++) {
      if (String(items[i].annotation_id || "") === String(annotationId)) {
        return items[i];
      }
    }
    return null;
  }

  // =========================================================================
  // §6.1/§6.2/§6.3 对象交互核心：
  //   - viewer 容器「捕获阶段」命中路由（一次 pointer 会话只交给一个处理器；
  //     只在确认对象编辑时消费事件，其余交给 OSD 平移。不给透明画布开
  //     常驻 pointer-events:auto）
  //   - 就近工具条（四种形态；出视窗翻转；⋯ 收纳低频动作）
  //   - 「保存中 / 已保存 / 未保存+重试」轻量状态
  //   - geometry_locked 锁定/解锁、polygon 编辑轮廓点、方向键移动、Delete
  // =========================================================================
  var viewerSpacePan = false;        // Space 按住 → 永远平移（优先于对象编辑）
  var pendingAnnoSelect = null;      // 未消费的按下（click 候选）：{hit, sx, sy, id}
  var EDIT_CLICK_SLOP_PX = 6;        // click/drag 区分阈值（CSS px，§6.2）

  // 是否可拖动/手柄编辑该对象：有编辑权且位置未锁（§6.3：锁定=冻结几何，
  // 仍可改备注/解锁；只读=无编辑权，无手柄）。
  function canDragItem(it) {
    return !!it && itemCanEdit(it) && !itemGeometryLocked(it);
  }

  function consumePointer(e) {
    if (e && e.preventDefault) e.preventDefault();
    if (e && e.stopPropagation) e.stopPropagation();
  }

  // 事件目标是查看器表面（瓦片画布）而非叠加 UI（工具条/保存状态/ROI 框/
  // 通道面板）时才参与对象路由（§6.2：UI 工具条优先级最高）。
  function isViewerSurfaceTarget(e) {
    var t = e && e.target;
    if (!t || !t.closest) return true;
    if (t.closest(".anno-ui-layer")) return false;
    if (t.closest("#roi-box")) return false;
    if (t.closest(".channel-panel")) return false;
    return true;
  }

  // ---- viewer 容器捕获阶段路由（§6.2 优先级：UI 工具条 > Space 平移 >
  //      显式绘制 > 可编辑选中对象手柄/本体 > 普通选择/平移） ----
  // UI 工具条是独立 DOM（pointer-events:auto），事件天然不落到 viewer 容器。
  function routeObjectPointerDown(e) {
    if (!state.slide || !viewer) return;
    if (e && e.button === 2) return;                 // 右键留上下文菜单
    if (!isViewerSurfaceTarget(e)) return;           // 叠加 UI 优先
    if (viewerSpacePan) return;                      // §6.2：Space 平移 > 显式绘制
    if (rectToolActive() || state.drawMode) return;  // 显式绘制：画布层自有路径
    if (!state.showAnno) return;
    var sp = screenPt(e);
    // 1) 已选中可编辑对象：控制点命中 → 调整（角双轴/边单轴/端点/包围框柄；
    //    §6.2「可直接拖控制点调整」——无需先点「编辑备注」）
    if (editItem && canDragItem(editItem)) {
      var hd = hitHandle(sp.x, sp.y, editItem);
      if (hd) {
        consumePointer(e);
        startEditDrag(e, editItem, hd, viewer.container);
        return;
      }
    }
    var hit = hitAnno(sp.x, sp.y);
    // 2) 已选中、未锁、可编辑的本体 → 移动整个对象（消费）
    if (hit && hit === editItem && canDragItem(hit)) {
      consumePointer(e);
      startEditDrag(e, hit, "move", viewer.container);
      updateAnnoToolbarPos();
      return;
    }
    // 3) 其它（未选中对象 / 锁定 / 只读 / 空白）：不消费——拖动=平移切片；
    //    若松手时位移极小则按单击处理（选择 / 显示锁定态 / 取消选择）。
    pendingAnnoSelect = { hit: hit, sx: e.clientX, sy: e.clientY, id: e.pointerId };
  }

  function routeObjectPointerMove(e) {
    if (pendingAnnoSelect && e.pointerId === pendingAnnoSelect.id &&
        Math.hypot(e.clientX - pendingAnnoSelect.sx,
                   e.clientY - pendingAnnoSelect.sy) > EDIT_CLICK_SLOP_PX) {
      pendingAnnoSelect = null;  // 已成拖动（OSD 平移），松手不再当单击
    }
    if (!editDrag || editDrag.captureEl !== viewer.container) return;
    consumePointer(e);
    applyEditDrag(e);
    updateAnnoToolbarPos();
  }

  function routeObjectPointerUp(e) {
    if (editDrag && editDrag.captureEl === viewer.container) {
      consumePointer(e);
      endEditDrag(e);
      return;
    }
    if (pendingAnnoSelect && e.pointerId === pendingAnnoSelect.id) {
      var sel = pendingAnnoSelect;
      pendingAnnoSelect = null;
      if (Math.hypot(e.clientX - sel.sx, e.clientY - sel.sy) > EDIT_CLICK_SLOP_PX) return;
      if (sel.hit) {
        if (sel.hit === editItem) {
          openAnnoToolbar();  // 再次单击已选中对象：保持选择，工具条已在
        } else {
          selectEditItem(sel.hit);  // 锁定/只读对象：选择并显示锁定/只读状态
        }
      } else {
        clearEditItem();  // 空白单击 → 取消选择
      }
    }
  }

  function routeObjectPointerCancel(e) {
    pendingAnnoSelect = null;
    if (editDrag && editDrag.captureEl === viewer.container) {
      cancelEditDragRestore();
    }
  }

  // polygon 编辑轮廓点：双击顶点删除（保持 ≥3 点）；完成时统一校验非自交
  function routeObjectDblClick(e) {
    if (!state.polygonVertexEdit || !editItem) return;
    var sp = screenPt(e);
    var hd = hitHandle(sp.x, sp.y, editItem);
    if (!hd || hd.id.charAt(0) !== "v") return;
    var vi = parseInt(hd.id.slice(1), 10) || 0;
    if (!editItem.points || editItem.points.length <= 3) {
      toast(t("anno.polygon.minpoints"), "info");
      return;
    }
    editItem.points.splice(vi, 1);
    consumePointer(e);
    redrawAnnoCanvas();
  }

  function togglePolygonVertexEdit(it) {
    if (!it || (it.type || "rect") !== "polygon" || !canDragItem(it)) {
      if (it) toast(t("anno.polygon.readonly"), "info");
      return;
    }
    if (state.polygonVertexEdit === it) { finishPolygonVertexEdit(it); return; }
    state.polygonVertexEdit = it;
    it.__vertexEditStart = snapshotGeom(it);
    // （顶点/加顶点柄由 editHandles 的 polygonVertexEdit 分支渲染；
    //   editing 只表示备注卡模式，不再承担柄显示职责）
    closeAnnoToolbarMenu();
    renderAnnoToolbar();
    redrawAnnoCanvas();
    toast(t("anno.polygon.editpoints.tip"), "info");
  }

  function finishPolygonVertexEdit(it) {
    var pts = (it.points || []).map(function (p) {
      return [Math.max(0, Math.round(p[0])), Math.max(0, Math.round(p[1]))];
    });
    if (pts.length < 3) { toast(t("anno.polygon.minpoints"), "error"); return; }
    if (AG.polygonSelfIntersects(pts)) {
      // §6.2：完成时校验非自交——不通过则留在编辑模式（不静默修正/提交）
      toast(t("anno.polygon.selfintersects"), "error");
      return;
    }
    state.polygonVertexEdit = null;
    var before = it.__vertexEditStart || snapshotGeom(it);
    it.__vertexEditStart = null;
    renderAnnoToolbar();
    redrawAnnoCanvas();
    saveItemGeometryIfChanged(it, before);
  }

  // Space 平移（输入框外）：按住即永远平移，即使指针经过对象（§6.2）
  function onSpaceKeyForPan(e) {
    var tgt = e.target;
    var tag = tgt && tgt.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" ||
        (tgt && tgt.isContentEditable)) return;
    if (e.key !== " ") return;
    if (e.type === "keydown" && !e.repeat) viewerSpacePan = true;
    else if (e.type === "keyup") viewerSpacePan = false;
  }

  // ---- 就近工具条（§6.1）----
  var annoToolbarEl = null;
  var annoToolbarMenuEl = null;
  var annoToolbarRenderKey = null;

  function ensureAnnoToolbar() {
    if (annoToolbarEl && annoToolbarEl.parentNode) return annoToolbarEl;
    annoToolbarEl = document.createElement("div");
    annoToolbarEl.id = "anno-obj-toolbar";
    annoToolbarEl.className = "anno-ui-layer";
    annoToolbarEl.hidden = true;
    // 工具条自身吃掉 pointer 事件（§6.2：UI 工具条优先级最高）
    annoToolbarEl.addEventListener("pointerdown", function (e) {
      e.stopPropagation();
    });
    if (viewer && viewer.container) viewer.container.appendChild(annoToolbarEl);
    return annoToolbarEl;
  }

  // 当前工具条目标："draft"（临时框）| 标注 item | null
  function annoToolbarTarget() {
    if (rectToolActive() && state.roi.w > 0) return "draft";
    return editItem;
  }

  function openAnnoToolbar(force) {
    var el = ensureAnnoToolbar();
    if (!el) return;
    var target = annoToolbarTarget();
    // 同一目标不重复重建（拖动每帧调用时避免 DOM 抖动）；锁态等变化由
    // 动作回调直接 renderAnnoToolbar() 刷新。
    var key = target === "draft" ? "draft"
      : (target ? String(target.annotation_id || "") + "|" + String(target.token || "") +
         "|" + Number(target.ts || 0) + "|" + (target.type || "rect") : null);
    if (!force && key !== null && key === annoToolbarRenderKey && el.childNodes.length) {
      el.hidden = false;
      updateAnnoToolbarPos();
      return;
    }
    annoToolbarRenderKey = key;
    renderAnnoToolbar();
    el.hidden = !target;
    updateAnnoToolbarPos();
  }

  function closeAnnoToolbar() {
    annoToolbarRenderKey = null;
    if (annoToolbarEl) { annoToolbarEl.hidden = true; }
    closeAnnoToolbarMenu();
  }

  function closeAnnoToolbarMenu() {
    if (annoToolbarMenuEl) { annoToolbarMenuEl.remove(); annoToolbarMenuEl = null; }
  }

  function annoToolbarButton(cls, text, title, onClick, disabled) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "aot-btn " + cls;
    b.textContent = text;
    if (title) b.title = title;
    if (disabled) b.disabled = true;
    if (onClick && !disabled) b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var menuWasOpen = !!annoToolbarMenuEl;
      closeAnnoToolbarMenu();
      onClick(menuWasOpen);
    });
    return b;
  }

  function renderAnnoToolbar() {
    var el = ensureAnnoToolbar();
    if (!el) return;
    closeAnnoToolbarMenu();
    el.innerHTML = "";
    var target = annoToolbarTarget();
    if (!target) { el.hidden = true; return; }
    el.hidden = false;

    if (target === "draft") {
      // 临时框：[保存为标记] [✨ 加入 AI 对话] [锁定位置] [⋯]
      // （ROI 附件 2026-10-18：未保存草稿可加入对话——次动作，保存仍为主动作；
      //   冻结点击时刻的 level-0 bbox，不伪造 annotation_id、不落库。）
      el.appendChild(annoToolbarButton("aot-save", t("anno.toolbar.save"),
        t("anno.toolbar.save.tip"), function () {
          submitCurrentDraft();
        }, drawPhase === "saving"));
      el.appendChild(annoAiAttachDraftToolbarButton());
      el.appendChild(annoToolbarButton("aot-lock" + (state.roiGeometryLocked ? " on" : ""),
        state.roiGeometryLocked ? "🔒 " + t("anno.lock.locked.short") : "🔒 " + t("anno.lock.lock"),
        state.roiGeometryLocked ? t("anno.lock.unlock") : t("anno.lock.lock.tip"),
        function () { toggleDraftGeometryLocked(); }));
      el.appendChild(annoToolbarButton("aot-more", "⋯", t("anno.toolbar.more"),
        function (menuWasOpen) { if (!menuWasOpen) openAnnoToolbarMenu("draft"); }));
      return;
    }

    var it = target;
    var editable = itemCanEdit(it);
    var locked = itemGeometryLocked(it);
    // 只读：可写动作不渲染，但「✨ 加入 AI 对话」不受 can_edit 限制——
    // 只读且有权查看的标记同样可以加入讨论（权限只约束写，不约束读/讨论）。
    if (!editable) {
      el.appendChild(annoToolbarButton("aot-readonly", "🔒 " + t("anno.lock.readonly"),
        t("anno.lock.readonly.tip"), null, true));
      el.appendChild(annoAiAttachToolbarButton(it));
      el.appendChild(annoToolbarButton("aot-more", "⋯", t("anno.toolbar.more"),
        function (menuWasOpen) { if (!menuWasOpen) openAnnoToolbarMenu(it); }));
      return;
    }
    // AI 待复核：[接受] [编辑备注] [✨ 加入 AI 对话] [⋯]
    if (itemIsAiPending(it)) {
      el.appendChild(annoToolbarButton("aot-accept", "✓ " + t("anno.review.accept"),
        t("anno.review.accept.tip"), function () { reviewAnnotation(it, "accept"); }));
    }
    el.appendChild(annoToolbarButton("aot-note", "✎ " + t("anno.toolbar.note"),
      t("anno.toolbar.note.tip"), function () {
        editing = true;
        openEditCard(it);
        redrawAnnoCanvas();
      }));
    // ✨ 加入 AI 对话：主条目（UX 改版 2026-10-18：从 ⋯ 提到工具条；锁定
    // 移入 ⋯）。冻结与发射在**点击时刻**执行——菜单打开不再预冻结。
    el.appendChild(annoAiAttachToolbarButton(it));
    // 锁定/解锁移入 ⋯ 菜单（openAnnoToolbarMenu），工具条不再占位。
    el.appendChild(annoToolbarButton("aot-more", "⋯", t("anno.toolbar.more"),
      function (menuWasOpen) { if (!menuWasOpen) openAnnoToolbarMenu(it); }));
  }

  // 工具条「✨ 加入 AI 对话」按钮（AI 入口统一 2026-10-18）：
  //   - 插件未加载 → 禁用 + 「插件未加载」说明；几何不可冻结（如 viewer 未
  //     就绪）→ 禁用 + 「暂无可加入的区域」说明——不假装加入成功；
  //   - 可用时点击才冻结（freezeMarkerAttachPayload 与右键菜单同一实现），
  //     经 emitConversationAttachIntent 进当前会话草稿：不发送、不启动模型、
  //     不新建会话、不覆盖输入框已有文字。
  function annoAiAttachToolbarButton(it) {
    var disabled = false;
    var tip = t("anno.menu.askai.tip");
    if (!hpReady()) {
      disabled = true;
      tip = t("ctxmenu.attach.plugin.missing");
    } else {
      try {
        if (!freezeMarkerAttachPayload(it, null)) {
          disabled = true;
          tip = t("anno.menu.askai.unavailable");
        }
      } catch (e) {
        disabled = true;
        tip = t("anno.menu.askai.unavailable");
      }
    }
    return annoToolbarButton("aot-askai", "✨ " + t("anno.menu.askai"), tip,
      function () { attachMarkerToConversation(it); }, disabled);
  }

  // 标记 → 当前 AI 会话草稿（点击时刻冻结；emitConversationAttachIntent 内
  // 部再校验 hpReady 并给 toast）。右键菜单与标注行 AI 动作共用。
  function attachMarkerToConversation(it) {
    if (!it || !hpReady()) {
      toast(t("ctxmenu.attach.plugin.missing"), "error");
      return;
    }
    var frozen = null;
    try { frozen = freezeMarkerAttachPayload(it, null); } catch (e) { frozen = null; }
    if (!frozen) {
      toast(t("anno.menu.askai.unavailable"), "error");
      return;
    }
    emitConversationAttachIntent(frozen);
  }

  // 工具条「✨ 加入 AI 对话」按钮——临时框（未保存草稿）版（ROI 附件
  // 2026-10-18）。禁用语义与标记版一致：插件未加载 / 几何冻结不出来
  // （草稿宽高非法）→ 禁用 + 明确 tooltip，不假装加入成功。次动作：
  // 「保存为标记」仍是主动作（renderAnnoToolbar draft 分支首位）。
  function annoAiAttachDraftToolbarButton() {
    var disabled = false;
    var tip = t("anno.menu.askai.draft.tip");
    if (!hpReady()) {
      disabled = true;
      tip = t("ctxmenu.attach.plugin.missing");
    } else {
      try {
        if (!freezeDraftAttachPayload()) {
          disabled = true;
          tip = t("anno.menu.askai.unavailable");
        }
      } catch (e) {
        disabled = true;
        tip = t("anno.menu.askai.unavailable");
      }
    }
    return annoToolbarButton("aot-askai aot-askai-draft", "✨ " + t("anno.menu.askai"), tip,
      function () { attachDraftToConversation(); }, disabled);
  }

  // 未保存草稿 → 当前 AI 会话草稿（点击时刻冻结 roi 载荷；此后草稿移动/
  // 缩放/保存都不影响已加入的附件——冻结的是独立快照）。
  function attachDraftToConversation() {
    if (!hpReady()) {
      toast(t("ctxmenu.attach.plugin.missing"), "error");
      return;
    }
    var frozen = null;
    try { frozen = freezeDraftAttachPayload(); } catch (e) { frozen = null; }
    if (!frozen) {
      toast(t("anno.menu.askai.unavailable"), "error");
      return;
    }
    emitConversationAttachIntent(frozen);
  }

  function openAnnoToolbarMenu(target) {
    closeAnnoToolbarMenu();
    var el = ensureAnnoToolbar();
    if (!el) return;
    var menu = document.createElement("div");
    menu.className = "aot-menu";
    function addItem(label, title, onClick, disabled, cls) {
      menu.appendChild(annoToolbarButton(
        "aot-menu-item" + (cls ? " " + cls : ""), label, title, onClick, disabled));
    }
    if (target === "draft") {
      addItem(t("anno.solo.off"), "", function () {
        state.focusAnno = null;
        redrawAnnoCanvas();
      }, !state.focusAnno);
      el.appendChild(menu);
      annoToolbarMenuEl = menu;
      return;
    }
    var it = target;
    // 「✨ 加入 AI 对话」已上移到工具条主位（renderAnnoToolbar）；⋯ 不再重复。
    // 「定位」动作按实际行为命名「居中显示此标记」（只导航 + 选中，不加入
    // AI 上下文；tooltip 写明「调整视野并高亮此标记」）。
    addItem("⌖ " + t("anno.menu.locate"), t("anno.menu.locate.tip"), function () {
      jumpToAnno(it);
    });
    // 只看此标记 / 显示全部标记（§6.1：与普通选择分开）
    var solo = (state.focusAnno === it);
    addItem(solo ? "👁 " + t("anno.solo.off") : "👁 " + t("anno.solo"),
      t("anno.solo.tip"), function () {
        state.focusAnno = solo ? null : it;
        redrawAnnoCanvas();
        renderAnnoToolbar();
      });
    // 锁定/解锁（UX 改版 2026-10-18：从工具条移入 ⋯；保留锁状态提示）。
    // 只读身份不能改锁——不渲染该项目（只读徽章已在工具条说明）。
    if (itemCanEdit(it)) {
      var lk = itemGeometryLocked(it);
      addItem(lk ? "🔓 " + t("anno.lock.unlock") : "🔒 " + t("anno.lock.lock"),
        lk ? t("anno.lock.unlock.tip") : t("anno.lock.lock.tip"),
        function () { toggleItemGeometryLocked(it, !lk); renderAnnoToolbar(); });
    }
    // polygon：编辑轮廓点（§6.2：⋯ 内入口；完成后校验非自交）
    if ((it.type || "rect") === "polygon" && itemCanEdit(it) && !itemGeometryLocked(it)) {
      var inEdit = (state.polygonVertexEdit === it);
      addItem(inEdit ? "✓ " + t("anno.polygon.finish") : "⬡ " + t("anno.polygon.editpoints"),
        t("anno.polygon.editpoints.tip"), function () {
          if (inEdit) { finishPolygonVertexEdit(it); }
          else { togglePolygonVertexEdit(it); }
          renderAnnoToolbar();
        });
    }
    // AI 待复核：驳回
    if (itemIsAiPending(it)) {
      addItem(t("anno.review.reject"), t("anno.review.reject.tip"), function () {
        reviewAnnotation(it, "reject");
      });
    }
    // 删除（锁定对象禁用：§6.3 Delete 对锁定无效，入口同理给说明）
    var delDisabled = itemGeometryLocked(it);
    addItem("🗑 " + t("edit.del"),
      delDisabled ? t("anno.lock.delete.denied") : t("anno.del.title"),
      function () { deleteAnnoItem(it); },
      delDisabled || !itemCanEdit(it));
    el.appendChild(menu);
    annoToolbarMenuEl = menu;
  }

  // 工具条定位：对象上方、靠左对齐外接框；顶部放不下翻到对象下方/内部；
  // 左右 clamp 在视窗内（§6.1 出视窗翻转）。
  function updateAnnoToolbarPos() {
    if (!annoToolbarEl || annoToolbarEl.hidden || !viewer) return;
    var target = annoToolbarTarget();
    if (!target) { annoToolbarEl.hidden = true; return; }
    var bb;
    if (target === "draft") {
      var r = state.roi;
      var q = AG.rectQuad(r.x, r.y, r.w, r.h, imgToCanvas);
      bb = AG.quadBBox(q);
      // DOM 标签 #roi-box .roi-label 位于框上方 24px，一并避开
      if (bb) bb = { x: bb.x, y: bb.y - 24, w: bb.w, h: bb.h + 24 };
    } else {
      bb = itemScreenBBox(target);
    }
    if (!bb) { annoToolbarEl.hidden = true; return; }
    var tw = annoToolbarEl.offsetWidth, th = annoToolbarEl.offsetHeight;
    if (!tw) {  // display 前测量：先挂上再量
      annoToolbarEl.style.visibility = "hidden";
      tw = annoToolbarEl.offsetWidth; th = annoToolbarEl.offsetHeight;
      annoToolbarEl.style.visibility = "";
    }
    var vw = viewer.container.clientWidth, vh = viewer.container.clientHeight;
    var x = clamp(bb.x, 8, Math.max(8, vw - tw - 8));
    var y = bb.y - th - 8;
    if (y < 8) y = bb.y + bb.h + 8;                       // 上方放不下 → 下方
    if (y + th > vh - 8) y = clamp(bb.y + 8, 8, Math.max(8, vh - th - 8)); // 仍放不下 → 对象内部
    annoToolbarEl.style.left = Math.round(x) + "px";
    annoToolbarEl.style.top = Math.round(y) + "px";
  }

  // 标签屏幕区域（与 drawLabel 同款锚点/尺寸：框高 16、底边距锚 2px）。
  // 就近工具条定位盒并入标签，避免盖住标记左上角的黄色标签（§6.1）。
  function labelScreenRegion(it, typ, bb) {
    if (!it || !it.label) return null;
    // rect 有备注时标签画在框内（drawLabel inside），不占外部空间
    if (typ === "rect" && String(it.note || "").trim()) return null;
    var ax, ay;
    if (typ === "rect") {
      ax = bb.x; ay = bb.y;
    } else if (typ === "arrow") {
      var b = imgToCanvas(it.x2, it.y2);
      ax = b.x + 6; ay = b.y - 6;
    } else {
      var p0 = imgToCanvas(it.points[0][0], it.points[0][1]);
      ax = p0.x; ay = p0.y;
    }
    var text = String(it.label);
    var w;
    if (annoCtx) {
      annoCtx.font = "600 11px " +
        "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif";
      w = annoCtx.measureText(text).width + 10;  // drawLabel padX * 2
    } else {
      w = text.length * 8 + 12;
    }
    return { x: ax - 2, y: ay - 20, w: w + 4, h: 20 };
  }

  function unionBBox(a, b) {
    if (!a) return b;
    if (!b) return a;
    var x = Math.min(a.x, b.x), y = Math.min(a.y, b.y);
    var r = Math.max(a.x + a.w, b.x + b.w), bo = Math.max(a.y + a.h, b.y + b.h);
    return { x: x, y: y, w: r - x, h: bo - y };
  }

  // 选中对象屏幕外接框（旋转/镜像取投影 bbox；§6.2）。
  // 含标签区域（§6.1：就近工具条定位需避开标签）。
  function itemScreenBBox(it) {
    var typ = it.type || "rect";
    var bb;
    if (typ === "rect") {
      bb = rectScreenQuad(it).bbox;
    } else {
      var pts;
      if (typ === "arrow") {
        var a = imgToCanvas(it.x1, it.y1), b = imgToCanvas(it.x2, it.y2);
        pts = [[a.x, a.y], [b.x, b.y]];
      } else {
        pts = (it.points || []).map(function (p) {
          var c = imgToCanvas(p[0], p[1]);
          return [c.x, c.y];
        });
      }
      bb = AG.quadBBox(pts);
    }
    if (!bb) return null;
    return unionBBox(bb, labelScreenRegion(it, typ, bb));
  }

  // ---- 保存状态（§6.4）：saving / saved / unsaved(+重试) ----
  var annoSaveStatusEl = null;
  var annoSaveRetryFn = null;
  var annoSaveStatusTimer = null;

  function ensureAnnoSaveStatus() {
    if (annoSaveStatusEl && annoSaveStatusEl.parentNode) return annoSaveStatusEl;
    annoSaveStatusEl = document.createElement("div");
    annoSaveStatusEl.className = "anno-save-status anno-ui-layer";
    annoSaveStatusEl.hidden = true;
    annoSaveStatusEl.addEventListener("pointerdown", function (e) {
      e.stopPropagation();
    });
    if (viewer && viewer.container) viewer.container.appendChild(annoSaveStatusEl);
    return annoSaveStatusEl;
  }

  function setAnnoSaveStatus(st, retryFn) {
    var el = ensureAnnoSaveStatus();
    if (!el) return;
    if (annoSaveStatusTimer) { clearTimeout(annoSaveStatusTimer); annoSaveStatusTimer = null; }
    annoSaveRetryFn = retryFn || null;
    if (!st) { el.hidden = true; el.textContent = ""; el.className = "anno-save-status"; return; }
    el.hidden = false;
    el.className = "anno-save-status " + st;
    if (st === "saving") {
      el.textContent = t("anno.savestatus.saving");
    } else if (st === "saved") {
      el.textContent = t("anno.savestatus.saved");
      annoSaveStatusTimer = setTimeout(function () { setAnnoSaveStatus(null); }, 1600);
    } else {
      el.textContent = t("anno.savestatus.unsaved");
      el.classList.add("clickable");
      el.onclick = function (ev) {
        ev.stopPropagation();
        if (typeof annoSaveRetryFn === "function") annoSaveRetryFn();
      };
    }
  }

  // ---- 几何差量比较 + 松手一次 PATCH（§6.2/§6.4：无有效位移不发） ----
  function geomEqual(a, b) {
    return JSON.stringify(normalizeGeomForCompare(a)) ===
           JSON.stringify(normalizeGeomForCompare(b));
  }
  function normalizeGeomForCompare(g) {
    if (!g) return {};
    var out = {};
    if (g.points) {
      out.points = g.points.map(function (p) {
        return [Math.round(Number(p[0]) || 0), Math.round(Number(p[1]) || 0)];
      });
      return out;
    }
    ["x", "y", "w", "h", "x1", "y1", "x2", "y2"].forEach(function (k) {
      if (g[k] != null) out[k] = Math.round(Number(g[k]) || 0);
    });
    return out;
  }

  function saveItemGeometryIfChanged(it, beforeGeom) {
    if (!it || !it.annotation_id) return;
    var after = buildEditGeom(it);
    if (geomEqual(beforeGeom, after)) return;  // 无有效位移：不发 PATCH
    sendItemGeometryPatch(it, after, beforeGeom, null);
  }

  // 手势/键盘几何保存：一次 PATCH（只带 geom，不带 note——差量更新，§6.3）；
  // 成功 → 撤销单元（服务器新 revision）；锁定/冲突按错误码区分（§6.4）。
  function sendItemGeometryPatch(it, geom, beforeGeom, extraBody) {
    if (sendItemGeometryPatch._inflight) return;  // 网络等待期间不启动第二个写手势
    var before = beforeGeom || snapshotGeom(it);
    var revision = Number(it.revision) > 0 ? Number(it.revision) : 0;
    var body = extraBody || {};
    body.geom = geom;
    if (revision > 0) body.expected_revision = revision;  // 新 UI 始终传 CAS
    sendItemGeometryPatch._inflight = true;
    setAnnoSaveStatus("saving");
    apiFetch(annoIdUrl(it.annotation_id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        return { ok: r.ok, status: r.status, j: j };
      });
    }).then(function (res) {
      sendItemGeometryPatch._inflight = false;
      if (res.ok) {
        if (res.j && res.j.revision != null) it.revision = res.j.revision;
        setAnnoSaveStatus("saved");
        // 撤销单元 = 这次几何编辑（before=手势起点快照；过锁与 revision 由
        // undo 路径的 409 处理承担，§6.3）
        pushUndoEntry({
          kind: "edit",
          slide: state.slide ? state.slide.name : null,
          slide_id: state.slide ? (state.slide.id || null) : null,
          userId: currentUserId,
          annotationId: it.annotation_id,
          revision: (res.j && res.j.revision != null) ? res.j.revision : it.revision,
          before: { geom: before, note: it.note != null ? it.note : "" },
          after: { geom: geom, note: it.note != null ? it.note : "" },
        });
        refreshCurrentAnnotations();
        return;
      }
      handleGeometrySaveError(it, res, geom, before);
    }).catch(function (e) {
      sendItemGeometryPatch._inflight = false;
      setAnnoSaveStatus("unsaved", function () {
        sendItemGeometryPatch(it, geom, before, null);
      });
      toast(t("save.fail2", { e: (e && e.message) || e }), "error");
    });
  }

  // 409 按错误码区分：annotation_locked（他人锁定→结束并恢复服务端态）≠
  // revision_conflict（保留本地草稿 + 读回服务端版本供比较，支持重试）。
  function handleGeometrySaveError(it, res, geom, before) {
    var j = res.j || {};
    if (res.status === 409 && j.error === "annotation_locked") {
      // 他人（或另一端）已锁定：不保留本地几何草稿，恢复服务端版本
      setAnnoSaveStatus(null);
      toast(t("anno.error.locked", {
        rev: j.current && j.current.revision != null ? j.current.revision : "?",
      }), "error");
      it.geometry_locked = true;
      refreshCurrentAnnotations();
      renderAnnoToolbar();
      redrawAnnoCanvas();
      return;
    }
    if (res.status === 409) {
      // revision_conflict：保留本地草稿；读回的服务端版本在 j.current
      var serverRev = j.current && j.current.revision != null
        ? j.current.revision : j.current_revision;
      setAnnoSaveStatus("unsaved", function () {
        // 显式重试：以读回的服务端 revision 作为新 CAS 基准
        if (serverRev != null) it.revision = serverRev;
        sendItemGeometryPatch(it, geom, before, null);
      });
      toast(t("edit.conflict.keep", { rev: serverRev != null ? serverRev : "?" }), "error");
      return;
    }
    setAnnoSaveStatus("unsaved", function () {
      sendItemGeometryPatch(it, geom, before, null);
    });
    toast(t("save.fail2", { e: j.error || res.status }), "error");
  }

  // ---- geometry_locked 切换（§6.3）：锁定/解锁只发锁字段，绝不夹带 geom ----
  function toggleItemGeometryLocked(it, lockTarget) {
    if (!it || !it.annotation_id) return;
    if (!itemCanEdit(it)) {
      toast(t("anno.lock.readonly.tip"), "info");  // 只读：隐藏/禁用解锁并说明原因
      return;
    }
    var revision = Number(it.revision) > 0 ? Number(it.revision) : 0;
    var body = { geometry_locked: !!lockTarget };
    if (revision > 0) body.expected_revision = revision;
    apiFetch(annoIdUrl(it.annotation_id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        return { ok: r.ok, status: r.status, j: j };
      });
    }).then(function (res) {
      if (res.ok) {
        it.geometry_locked = !!lockTarget;
        if (res.j && res.j.revision != null) it.revision = res.j.revision;
        toast(lockTarget ? t("anno.lock.locked") : t("anno.lock.unlocked"), "success");
        if (editBeforeSnapshot && editBeforeSnapshot.annotationId === it.annotation_id) {
          editBeforeSnapshot.geometryLocked = !!lockTarget;
        }
        renderAnnoToolbar();
        if (annoPanelOpen) renderAnnoPanel((currentAnnotations || {}).annotations || []);
        redrawAnnoCanvas();
        return;
      }
      var j = res.j || {};
      if (res.status === 409 && j.error === "annotation_locked") {
        toast(t("anno.error.locked", {
          rev: j.current && j.current.revision != null ? j.current.revision : "?",
        }), "error");
      } else if (res.status === 409) {
        toast(t("edit.conflict", {
          rev: (j.current && j.current.revision) || j.current_revision || "?",
        }), "error");
      } else {
        toast(t("anno.update.fail3", { e: j.error || res.status }), "error");
      }
      refreshCurrentAnnotations();
    }).catch(function (e) {
      toast(t("anno.update.fail3", { e: (e && e.message) || e }), "error");
    });
  }

  // 临时框的位置锁（§6.3：draft.geometryLocked 只对当前草稿有效；
  // 保存为标记时带上，刷新画面不会意外解锁——锁字段随后端记录走）
  function toggleDraftGeometryLocked() {
    state.roiGeometryLocked = !state.roiGeometryLocked;
    renderAnnoToolbar();
    toast(state.roiGeometryLocked ? t("anno.lock.locked") : t("anno.lock.unlocked"),
          "info");
  }

  // ---- 键盘（§6.2）：方向键 1px / Shift 10px 合并为一个撤销单元；
  //      Delete 对锁定无效；Esc 撤销未提交几何 ----
  var kbEditSession = null;   // {it, before, timer}
  var KB_PATCH_DEBOUNCE_MS = 500;

  function kbNudge(it, dx, dy) {
    var typ = it.type || "rect";
    if (typ === "rect") {
      var W = state.slide.width, H = state.slide.height;
      var w = rectItemW(it), h = rectItemH(it);
      it.x = clamp(Math.round(it.x + dx), 0, Math.max(0, W - w));
      it.y = clamp(Math.round(it.y + dy), 0, Math.max(0, H - h));
    } else if (typ === "arrow") {
      it.x1 = Math.max(0, Math.round(it.x1 + dx));
      it.y1 = Math.max(0, Math.round(it.y1 + dy));
      it.x2 = Math.max(0, Math.round(it.x2 + dx));
      it.y2 = Math.max(0, Math.round(it.y2 + dy));
    } else if (typ === "freehand" || typ === "polygon") {
      var moved = AG.translatePoints(it.points || [], dx, dy);
      var shift = AG.pointsShiftIntoRect(moved, state.slide.width, state.slide.height);
      if (shift) moved = AG.translatePoints(moved, shift.dx, shift.dy);
      it.points = moved;
    }
    redrawAnnoCanvas();
    updateAnnoToolbarPos();
  }

  function onViewerArrowNudge(e) {
    if (!editItem || !state.slide) return false;
    if (!canDragItem(editItem)) {
      // 锁定/只读：提示原因，不移动（§6.2 键盘同样过锁）
      toast(itemCanEdit(editItem) ? t("anno.keyboard.locked") : t("anno.lock.readonly.tip"),
            "info");
      return true;
    }
    if (!kbEditSession || kbEditSession.it !== editItem) {
      // 新会话：快照起点；重复按键在提交前合并为同一个撤销单元
      kbEditSession = { it: editItem, before: snapshotGeom(editItem), timer: null };
    }
    var step = e.shiftKey ? 10 : 1;
    var dx = 0, dy = 0;
    if (e.key === "ArrowLeft") dx = -step;
    else if (e.key === "ArrowRight") dx = step;
    else if (e.key === "ArrowUp") dy = -step;
    else if (e.key === "ArrowDown") dy = step;
    else return false;
    e.preventDefault();
    kbNudge(editItem, dx, dy);
    // 松键后一次性 PATCH（合并连续按键）；撤销单元 = 会话起点 → 提交值
    if (kbEditSession.timer) clearTimeout(kbEditSession.timer);
    var itRef = editItem;
    kbEditSession.timer = setTimeout(function () {
      var sess = kbEditSession;
      kbEditSession = null;
      if (sess && sess.it) saveItemGeometryIfChanged(sess.it, sess.before);
    }, KB_PATCH_DEBOUNCE_MS);
    return true;
  }

  function cancelKbEditSession(restore) {
    if (!kbEditSession) return false;
    var sess = kbEditSession;
    kbEditSession = null;
    if (sess.timer) clearTimeout(sess.timer);
    if (restore && sess.it) {
      var s = sess.before, it = sess.it;
      var typ = it.type || "rect";
      if (typ === "rect") {
        it.x = s.x; it.y = s.y; it.w = s.w; it.h = s.h;
      } else if (typ === "arrow") {
        it.x1 = s.x1; it.y1 = s.y1; it.x2 = s.x2; it.y2 = s.y2;
      } else {
        it.points = s.points;
      }
      redrawAnnoCanvas();
      updateAnnoToolbarPos();
    }
    return true;
  }

  // =========================================================================
  // 工单 D：Ctrl/Cmd+Z 撤销 / Ctrl/Cmd+Shift+Z（及 Ctrl+Y）重做
  // 撤销单元 = 语义操作（创建/编辑），不是每次 pointermove。
  // 栈限定：当前身份 + 当前切片 + 本地发起的操作（他人更新不入栈，也绝不
  // 被撤销）。创建的逆 = DELETE（expected_revision CAS）；编辑的逆 = PATCH
  // 回上一版几何/备注（expected_revision CAS）；409 → 不覆盖，toast 冲突。
  // =========================================================================
  var undoStack = [];
  var redoStack = [];
  var pendingUndoAfterSave = false; // 保存进行中收到撤销意图 → 成功后补执行

  function pushUndoEntry(fields) {
    redoStack.length = 0; // 新操作清空重做栈（标准撤销语义）
    var entry = fields;
    undoStack.push(entry);
    return entry;
  }

  function undoEntryInScope(entry) {
    if (!entry) return false;
    // P2：切片作用域按 slide_id 比对；旧条目（无 slide_id）/旧后端回落名比对
    if (entry.slide_id && state.slide && state.slide.id) {
      if (String(entry.slide_id) !== String(state.slide.id)) return false;
    } else if (entry.slide && state.slide && entry.slide !== state.slide.name) {
      return false;
    }
    if (entry.userId != null && currentUserId != null &&
        String(entry.userId) !== String(currentUserId)) return false;
    return true;
  }

  // 弹出栈顶直到找到仍属当前身份/切片的条目（切换后旧条目直接作废）
  function popScoped(stack) {
    while (stack.length) {
      var top = stack[stack.length - 1];
      if (undoEntryInScope(top)) return stack.pop();
      stack.pop();
    }
    return null;
  }

  function annoIdUrl(annotationId, suffix) {
    var path = "/api/annotation/id/" + encodeURIComponent(annotationId);
    return suffix ? path + suffix : path;
  }

  function sendAnnoDelete(entry, expectedRevision) {
    if (!entry || !entry.annotationId) {
      return Promise.resolve({ ok: false, status: 404, error: "missing_id" });
    }
    var body = {};
    if (expectedRevision != null) body.expected_revision = expectedRevision;
    return apiFetch(annoIdUrl(entry.annotationId), {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (r.ok) {
        return r.json().then(function (j) {
          // P3 研究采集：删除业务写入成功才记事件（显式删除与撤销创建
          // 同一 DELETE 入口；§7.1 匿名局部标注 ID，无几何字段）
          if (researchTelemetry) {
            researchTelemetry.notifyAnnotation({
              action: "annotation_delete",
              annotationId: entry.annotationId,
            });
          }
          return { ok: true, status: r.status, revision: j && j.revision };
        }).catch(function () { return { ok: true, status: r.status }; });
      }
      return r.json().catch(function () { return {}; }).then(function (j) {
        return { ok: false, status: r.status, error: j && j.error,
                 errorCode: j && j.error,
                 conflict: r.status === 409,
                 locked: r.status === 409 && j && j.error === "annotation_locked",
                 currentRevision: j && ((j.current && j.current.revision) ||
                                        j.current_revision) };
      });
    });
  }

  function sendAnnoPatch(entry, geom, note, expectedRevision) {
    if (!entry || !entry.annotationId) {
      return Promise.resolve({ ok: false, status: 404, error: "missing_id" });
    }
    var body = { geom: geom, note: note };
    if (expectedRevision != null) body.expected_revision = expectedRevision;
    return apiFetch(annoIdUrl(entry.annotationId), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (r.ok) {
        return r.json().then(function (j) {
          return { ok: true, status: r.status, revision: j && j.revision,
                   annotationId: j && j.annotation_id };
        }).catch(function () { return { ok: true, status: r.status }; });
      }
      return r.json().catch(function () { return {}; }).then(function (j) {
        return { ok: false, status: r.status, error: j && j.error,
                 errorCode: j && j.error,
                 conflict: r.status === 409,
                 locked: r.status === 409 && j && j.error === "annotation_locked",
                 currentRevision: j && ((j.current && j.current.revision) ||
                                        j.current_revision) };
      });
    });
  }

  // 撤销「创建」：按稳定 annotation_id 删除。目标不存在视为已撤销，绝不回退 index。
  function undoCreateEntry(entry) {
    if (!entry.annotationId) return Promise.resolve(true);
    var rev = Number(entry.revision) > 0 ? Number(entry.revision) : null;
    return sendAnnoDelete(entry, rev).then(function (res) {
      if (res.ok || res.status === 404) {
        if (res.ok && res.revision != null) entry.revision = res.revision;
        refreshAfterUndo(entry);
        return true;
      }
      if (res.locked) {
        // §6.3：撤销过锁——创建后被人锁定，删除（逆操作）被拒，提示先解锁
        toast(t("anno.error.locked.undo", {
          rev: res.currentRevision != null ? res.currentRevision : "?",
        }), "error");
        return false;
      }
      if (res.conflict) {
        toast(t("undo.conflict", {
          rev: res.currentRevision != null ? res.currentRevision : "?",
        }), "error");
        return false;
      }
      toast(t("undo.fail", { e: res.error || res.status }), "error");
      return false;
    });
  }

  // PATCH geom 清洗：去掉 side_px（v2 w/h 与 side_px 冲突会被 store 拒绝；
  // 快照兼容旧字段的读取，但提交只走 v2 成对 w/h）
  function cleanPatchGeom(g) {
    var out = {};
    for (var k in g) {
      if (k === "side_px") continue;
      out[k] = g[k];
    }
    return out;
  }

  // 撤销「编辑」：PATCH 回上一版；CAS 用本次操作完成后的 revision，不用列表最新值。
  // §6.3：撤销过锁——他人后来加锁时 409 annotation_locked，提示先解锁，不绕过。
  function undoEditEntry(entry) {
    if (!entry.annotationId) {
      toast(t("undo.fail", { e: "" }), "error");
      return Promise.resolve(false);
    }
    var rev = Number(entry.revision) > 0 ? Number(entry.revision) : null;
    return sendAnnoPatch(entry, cleanPatchGeom(entry.before.geom),
                         entry.before.note, rev)
      .then(function (res) {
        if (res.ok) {
          if (res.revision != null) entry.revision = res.revision;
          refreshAfterUndo(entry);
          return true;
        }
        if (res.locked) {
          toast(t("anno.error.locked.undo", {
            rev: res.currentRevision != null ? res.currentRevision : "?",
          }), "error");
          return false;
        }
        if (res.conflict) {
          toast(t("undo.conflict", {
            rev: res.currentRevision != null ? res.currentRevision : "?",
          }), "error");
          return false;
        }
        toast(t("undo.fail", { e: res.error || res.status }), "error");
        return false;
      });
  }

  function refreshAfterUndo(entry) {
    refreshCurrentAnnotations();
    loadAnnotationsIndex().then(function () {
      renderFolderBrowser();
    });
  }

  function performUndo() {
    if (drawPhase === "saving") {
      // 保存进行中：记录意图，成功后补执行（不能只隐藏前端对象）
      pendingUndoAfterSave = true;
      toast(t("undo.pending"), "info");
      return;
    }
    if (undoWhileDrawing()) return; // 绘制中：先撤草稿/上一控制点
    var entry = popScoped(undoStack);
    if (!entry) return;
    var p = entry.kind === "create" ? undoCreateEntry(entry) : undoEditEntry(entry);
    p.then(function (ok) {
      if (ok) {
        redoStack.push(entry);
        toast(t("undo.done"), "success");
      } else {
        undoStack.push(entry); // 冲突/失败：条目留在撤销栈
      }
    });
  }

  function performRedo() {
    if (drawPhase === "saving") return;
    var entry = popScoped(redoStack);
    if (!entry) return;
    var p;
    if (entry.kind === "create") {
      // 重做创建：恢复原 annotation_id 的 tombstone，不重用 client_action_id 再 INSERT。
      if (!entry.annotationId) {
        toast(t("redo.fail", { e: "" }), "error");
        redoStack.push(entry);
        return;
      }
      var restoreBody = {};
      if (Number(entry.revision) > 0) restoreBody.expected_revision = Number(entry.revision);
      p = apiFetch(annoIdUrl(entry.annotationId, "/restore"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(restoreBody),
      }).then(function (r) {
        if (!r.ok) return r.json().catch(function () { return {}; }).then(function (j) {
          return { ok: false, error: j && j.error, status: r.status,
                   conflict: r.status === 409, currentRevision: j && j.current_revision };
        });
        return r.json().then(function (j) { return { ok: true, j: j }; });
      }).then(function (res) {
        if (res.ok) {
          if (res.j && res.j.revision != null) entry.revision = res.j.revision;
          refreshAfterUndo(entry);
          return true;
        }
        if (res.conflict) {
          toast(t("undo.conflict", {
            rev: res.currentRevision != null ? res.currentRevision : "?",
          }), "error");
          return false;
        }
        toast(t("redo.fail", { e: res.error || res.status }), "error");
        return false;
      });
    } else {
      if (!entry.annotationId) {
        toast(t("redo.fail", { e: "" }), "error");
        redoStack.push(entry);
        return;
      }
      var rev = Number(entry.revision) > 0 ? Number(entry.revision) : null;
      p = sendAnnoPatch(entry, entry.after.geom, entry.after.note, rev)
        .then(function (res) {
          if (res.ok) {
            if (res.revision != null) entry.revision = res.revision;
            refreshAfterUndo(entry);
            return true;
          }
          if (res.conflict) {
            toast(t("undo.conflict", {
              rev: res.currentRevision != null ? res.currentRevision : "?",
            }), "error");
            return false;
          }
          toast(t("redo.fail", { e: res.error || res.status }), "error");
          return false;
        });
    }
    p.then(function (ok) {
      if (ok) {
        undoStack.push(entry);
        toast(t("redo.done"), "success");
      } else {
        redoStack.push(entry);
      }
    });
  }

  function canUndo() { return undoStack.some(undoEntryInScope); }
  function canRedo() { return redoStack.some(undoEntryInScope); }

  // 查看器级键盘（工单 D）：Escape / Enter / Ctrl(Cmd)+Z / +Shift+Z / Ctrl+Y。
  // 输入控件（含 contenteditable）内不劫持——保留原生文字撤销与表单行为。
  function onViewerKeydown(e) {
    var tgt = e.target;
    var tag = tgt && tgt.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" ||
        (tgt && tgt.isContentEditable)) return;
    var mod = !!(e.ctrlKey || e.metaKey);
    if (mod && !e.altKey && (e.key === "z" || e.key === "Z")) {
      e.preventDefault();
      if (e.shiftKey) { performRedo(); } else { performUndo(); }
      return;
    }
    if (mod && !e.altKey && (e.key === "y" || e.key === "Y")) {
      e.preventDefault();
      performRedo();
      return;
    }
    // §6.2：方向键移动选中对象（1 / Shift 10 个 level-0 px，合并提交）
    if (!mod && !e.altKey &&
        (e.key === "ArrowLeft" || e.key === "ArrowRight" ||
         e.key === "ArrowUp" || e.key === "ArrowDown")) {
      onViewerArrowNudge(e);
      return;
    }
    // §6.2：Delete 删除选中对象；对锁定对象不生效（给说明）
    if ((e.key === "Delete" || e.key === "Backspace") && editItem) {
      e.preventDefault();
      if (!itemCanEdit(editItem)) {
        toast(t("anno.lock.readonly.tip"), "info");
      } else if (itemGeometryLocked(editItem)) {
        toast(t("anno.lock.delete.denied"), "info");
      } else {
        deleteAnnoItem(editItem);
      }
      return;
    }
    if (e.key === "Escape") {
      if (rectToolActive()) { onRectKeydown(e); return; }
      if (state.drawMode) {
        e.preventDefault();
        if (drawPreview || retryDraft) {
          // 有当前绘制/失败草稿 → 只撤草稿，留在工具内
          drawPreview = null;
          drawPointer = null;
          retryDraft = null;
          setDrawUnsaved(false);
          setDrawPhase("idle");
          redrawAnnoCanvas();
          toast(t("draw.cancelled"), "info");
        } else {
          exitDrawMode();
        }
      }
      // §6.2：Esc 撤销本次未提交几何（优先级：轮廓编辑 > 键盘会话 > 编辑态脏几何）
      if (state.polygonVertexEdit && editItem) {
        e.preventDefault();
        var pit = state.polygonVertexEdit;
        if (pit.__vertexEditStart) {
          pit.points = pit.__vertexEditStart.points;
          pit.__vertexEditStart = null;
        }
        state.polygonVertexEdit = null;
        editing = false;
        renderAnnoToolbar();
        redrawAnnoCanvas();
        toast(t("draw.cancelled"), "info");
        return;
      }
      if (cancelKbEditSession(true)) {
        e.preventDefault();
        toast(t("draw.cancelled"), "info");
        return;
      }
      if (editItem && editBeforeSnapshot &&
          !geomEqual(editBeforeSnapshot.geom, buildEditGeom(editItem))) {
        e.preventDefault();
        var s0 = editBeforeSnapshot.geom, it0 = editItem;
        var typ0 = it0.type || "rect";
        if (typ0 === "rect") {
          it0.x = s0.x; it0.y = s0.y; it0.w = s0.w; it0.h = s0.h;
        } else if (typ0 === "arrow") {
          it0.x1 = s0.x1; it0.y1 = s0.y1; it0.x2 = s0.x2; it0.y2 = s0.y2;
        } else {
          it0.points = s0.points;
        }
        redrawAnnoCanvas();
        toast(t("draw.cancelled"), "info");
      }
      return;
    }
    if (e.key === "Enter") {
      if (rectToolActive()) { onRectKeydown(e); return; }
      if (state.drawMode) {
        // Enter 只提交「有效未提交草稿」（armed 箭头 / 保存失败重试）
        var draft = collectPendingDraft();
        if (draft) {
          e.preventDefault();
          submitAnnotationDraft(draft);
        }
      } else if (state.polygonVertexEdit && editItem) {
        e.preventDefault();
        finishPolygonVertexEdit(editItem);
      }
    }
  }

  // ---------- 标注列表加载失败态（P3 2026-10-19） ----------
  // 失败详情（anno.panel.load.error 的 {e}）：HTTP 状态优先，网络断开/
  // 响应非 JSON 等无状态失败回落网络异常文案。
  function annoLoadFailDetail(e) {
    if (e && e.status) return "HTTP " + e.status;
    return t("anno.panel.load.net");
  }

  // 拉取当前切片标注（openSlide 打开切片后调用；面板错误态的重试按钮复用）。
  // 迟到响应防护（P2 复核 2026-10-18）：切片对象身份只判「换片」，判不了
  // 同一切片内两个请求的新旧——load 与 refresh 共用 annoListReqGen 世代：
  // 新请求发出即 +1，响应到达时、JSON 解析后与 catch 内都复核，世代已旧
  // （同切片内更新的 load/refresh 已发出，或已换片）整体静默丢弃，成功与
  // 失败都不例外。
  // 非 401 失败（网络断开/5xx/403/非 JSON 体）进入 annoListError 显式错误
  // 态：面板按钮放开供查看错误与重试，绝不渲染成「暂无标注」冒充空切片。
  // 例外（review 047 #1）：当前切片已有成功数据（如重试失败时较新的刷新已
  // 先落地）→ 保留数据与选中，面板顶部插失效提示（stale），绝不把
  // currentAnnotations 置 null 清空用户正在看的内容。
  // 401(auth_required) 由 apiFetch 统一跳登录：静默退出，不闪错误 UI。
  function loadCurrentSlideAnnotations() {
    if (!state.slide) return Promise.resolve();
    var slideAtReq = state.slide;
    var genAtReq = ++annoListReqGen;
    annoListError = null;
    els.annoBtn.disabled = true;
    return apiFetch(annotationsQueryFor(state.slide))
      .then(function (r) {
        if (genAtReq !== annoListReqGen || state.slide !== slideAtReq) {
          throw { annoStaleReq: true }; // 迟到旧请求：连状态码都不再消费
        }
        if (r.status === 401) throw { annoSilent: true };
        if (!r.ok) throw { status: r.status };
        return r.json();
      })
      .then(function (data) {
        if (genAtReq !== annoListReqGen || state.slide !== slideAtReq) return;
        currentAnnotations = data;
        currentAnnotationsSlide = slideAtReq;
        annoListError = null;
        var annos = data.annotations || [];
        if (annos.length > 0) {
          els.annoBtn.disabled = false;
          els.annoAllBtn.disabled = false;
        }
        editItem = null;
        state.focusAnno = null;
        editing = false;
        rebuildFlatItems();
        redrawAnnoCanvas();
        if (annoPanelOpen) renderAnnoPanel(annos);
      })
      .catch(function (e) {
        if (e && e.annoStaleReq) return;        // 迟到的旧世代响应：静默丢弃
        if (e && e.annoSilent) return;          // 401：页面即将跳 /login
        if (genAtReq !== annoListReqGen || state.slide !== slideAtReq) return;
        if (currentAnnotations && currentAnnotationsSlide === slideAtReq) {
          // 本切片已有成功数据（较新的刷新/加载先落地）：保留数据走失效
          // 提示，绝不置 null 清空（review 047 #1）
          annoListStale = true;
          els.annoBtn.disabled = false;
          if (annoPanelOpen) renderAnnoPanelStale();
          return;
        }
        currentAnnotations = null;
        currentAnnotationsSlide = null;
        editItem = null;
        state.focusAnno = null;
        editing = false;
        rebuildFlatItems();
        redrawAnnoCanvas();
        annoListError = { status: (e && e.status) || 0 };
        // 「显示全部标记」无数据可叠加保持禁用；面板钮放开，用户可打开面板
        // 看到显式错误并重试（修复前按钮永禁用 + 面板显示「暂无标注」）
        els.annoAllBtn.disabled = true;
        els.annoBtn.disabled = false;
        if (annoPanelOpen) renderAnnoPanelError();
      });
  }

  // 面板内显式错误态（当前切片列表从未加载成功）：文案 + 重试按钮
  function renderAnnoPanelError() {
    els.annoPanelList.innerHTML = "";
    var box = document.createElement("div");
    box.className = "anno-panel-error";
    var msg = document.createElement("div");
    msg.className = "anno-panel-error-msg";
    msg.textContent = t("anno.panel.load.error",
      { e: annoLoadFailDetail(annoListError) });
    var retry = document.createElement("button");
    retry.className = "anno-panel-retry";
    retry.type = "button";
    retry.textContent = t("anno.panel.load.retry");
    retry.addEventListener("click", function () {
      retry.disabled = true;
      loadCurrentSlideAnnotations();
    });
    box.appendChild(msg);
    box.appendChild(retry);
    els.annoPanelList.appendChild(box);
  }

  // 刷新失败但面板正展示旧数据：顶部插失效提示条（保留既有行，绝不清空
  // 重画——瞬时失败不能把用户正在看的列表抹掉）。
  function renderAnnoPanelStale() {
    var prev = els.annoPanelList.querySelector(".anno-panel-stale");
    if (prev) prev.remove();
    var box = document.createElement("div");
    box.className = "anno-panel-stale";
    var msg = document.createElement("span");
    msg.className = "anno-panel-stale-msg";
    msg.textContent = t("anno.panel.stale");
    var retry = document.createElement("button");
    retry.className = "anno-panel-retry";
    retry.type = "button";
    retry.textContent = t("anno.panel.load.retry");
    retry.addEventListener("click", function () {
      retry.disabled = true;
      refreshCurrentAnnotations();
    });
    box.appendChild(msg);
    box.appendChild(retry);
    els.annoPanelList.insertBefore(box, els.annoPanelList.firstChild);
  }

  // 重新拉取当前切片标注并重绘（工单 D：返回 Promise 供「选中新标注」等待）
  function refreshCurrentAnnotations() {
    if (!state.slide) { redrawAnnoCanvas(); return Promise.resolve(); }
    // 迟到响应防护（P2 复核 2026-10-18）：与 loadCurrentSlideAnnotations
    // 共用 annoListReqGen 世代——请求发出即 +1，响应到达时、JSON 解析后与
    // catch 内复核，世代已旧（同切片内更新的 load/refresh 已发出，或已换
    // 片）整体静默丢弃，成功与失败都不例外；响应发出后用户换片（state.slide
    // 已是另一对象）同样丢弃，绝不让旧数据覆盖当前面板/画布。
    var slideAtReq = state.slide;
    var genAtReq = ++annoListReqGen;
    return apiFetch(annotationsQueryFor(state.slide))
      .then(function (r) {
        if (genAtReq !== annoListReqGen || state.slide !== slideAtReq) {
          throw { annoStaleReq: true }; // 迟到旧请求：连状态码都不再消费
        }
        if (r.status === 401) throw { annoSilent: true };
        if (!r.ok) throw { status: r.status };
        return r.json();
      })
      .then(function (data) {
        if (genAtReq !== annoListReqGen || state.slide !== slideAtReq) return;
        // 刷新成功即清两个失败态（先于 editDrag/kbEditSession 早退：即便
        // 本轮数据因手势不落地，错误态也不滞留——手势结束后的保存路径会
        // 再刷新）。否则「初始失败 → 手动新建保存成功 → 重开面板」仍会
        // 渲染错误块（review e64acaf3 #1）。
        annoListError = null;
        annoListStale = false;
        // 拖动/键盘编辑进行中：不重建 flatItems（item 引用会被替换，
        // 正在进行的 applyEditDrag 会改到孤儿对象）；结束后的保存路径会再刷新
        if (editDrag || kbEditSession) return;
        currentAnnotations = data;
        currentAnnotationsSlide = slideAtReq;
        var annos = data.annotations || [];
        els.annoBtn.disabled = annos.length === 0;
        els.annoAllBtn.disabled = annos.length === 0;
        if (annos.length === 0) { state.showAnno = false; syncAnnoAllBtns(); }
        rebuildFlatItems();
        // 选中跨刷新保持（§6.4：刷新后仍指向同一 annotation_id 的新副本）
        if (editItem && editItem.annotation_id) {
          var relink = findItemByAnnotationId(editItem.annotation_id);
          if (relink && relink !== editItem) {
            var localGeom = buildEditGeom(editItem);
            editItem = relink;
            // 冲突保留本地草稿（§6.4）：服务端刷新不抹掉未提交的本地几何
            if (editBeforeSnapshot &&
                !geomEqual(editBeforeSnapshot.geom, localGeom)) {
              applyGeomToItem(relink, localGeom);
            }
          } else if (!relink) {
            editItem = null; editing = false;
            state.polygonVertexEdit = null;
            closeAnnoToolbar();
          }
        }
        if (editItem && flatItems.indexOf(editItem) < 0) { editItem = null; editing = false; }
        // focusAnno 引用失效（flatItems 重建）→ 清空，恢复显示全部
        if (state.focusAnno && flatItems.indexOf(state.focusAnno) < 0) { state.focusAnno = null; }
        // 面板已开着：随数据刷新重渲——乐观更新（如删除）只画到刷新落地为
        // 止，之后由服务端真实数据接管；新标注到达也不再滞留旧列表。
        if (annoPanelOpen) renderAnnoPanel(annos);
        // 侧栏徽标与面板同源（P2 复核 2026-10-18）：本切片的新鲜分组写回
        // 标注索引的展示键（annoBadgeText 按名索引的同一解析），总数或分组
        // 数变了才重渲侧栏——徽标计数不再滞后于面板，也不加额外请求。
        try {
          var sinfo = findSlideInfo(slideAtReq.id || slideAtReq.name);
          var idxKey = (sinfo && (sinfo.name || sinfo.original_filename)) ||
            slideAtReq.name;
          if (allAnnotationsBySlide && idxKey) {
            var prev = allAnnotationsBySlide[idxKey] || [];
            var prevTotal = 0, nextTotal = 0;
            prev.forEach(function (g) { prevTotal += g.count || 0; });
            annos.forEach(function (g) { nextTotal += g.count || 0; });
            allAnnotationsBySlide[idxKey] = annos;
            if (prevTotal !== nextTotal || prev.length !== annos.length) {
              renderFolderBrowser();
            }
          }
        } catch (e) { /* 索引未就绪/解析失败：徽标按旧数据，下轮刷新再对齐 */ }
        redrawAnnoCanvas();
        updateAnnoToolbarPos();
      })
      .catch(function (e) {
        if (e && e.annoStaleReq) return;        // 迟到的旧世代响应：静默丢弃
        if (e && e.annoSilent) return;          // 401：页面即将跳 /login
        if (genAtReq !== annoListReqGen || state.slide !== slideAtReq) return;
        if (!currentAnnotations) {
          // 仍在「从未加载成功」的错误态（无旧数据可保留）：维持错误态——
          // 更新失败详情并重渲错误块，不标失效（stale 语义是「展示的是
          // 上次成功数据」，此处没有上次数据，stale 条叠在错误块上只会
          // 自相矛盾）（review e64acaf3 #2）。
          annoListError = { status: (e && e.status) || 0 };
          if (annoPanelOpen) renderAnnoPanelError();
          return;
        }
        // 非 401 刷新失败（P3 2026-10-19）：currentAnnotations 保持上次
        // 成功数据不动（失效数据优于伪装成空/清空面板），面板打开时顶部
        // 插失效提示 + 重试；面板关着时记状态，下次打开随行渲染。
        annoListStale = true;
        if (annoPanelOpen) renderAnnoPanelStale();
      });
  }

  // 把几何字段写回 item（rect/arrow/freehand/polygon；撤销/冲突恢复共用）
  function applyGeomToItem(it, g) {
    var typ = it.type || "rect";
    if (typ === "rect") {
      if (g.x != null) it.x = g.x;
      if (g.y != null) it.y = g.y;
      if (g.w != null) it.w = g.w;
      if (g.h != null) it.h = g.h;
    } else if (typ === "arrow") {
      if (g.x1 != null) it.x1 = g.x1;
      if (g.y1 != null) it.y1 = g.y1;
      if (g.x2 != null) it.x2 = g.x2;
      if (g.y2 != null) it.y2 = g.y2;
    } else if (g.points) {
      it.points = g.points;
    }
  }

  // =========================================================================
  // 标注面板 + 全部标记叠加（查看器）
  // =========================================================================
  function openAnnoPanel() {
    if (!state.slide) { toast(t("anno.none.current"), "info"); return; }
    if (!currentAnnotations && !annoListError) {
      // 列表尚未成功加载（首拉在途/401 跳转中）：维持旧提示，面板不开
      toast(t("anno.none.current"), "info");
      return;
    }
    // 开标注面板时关闭 AI 面板（协调权交给插件：发 panel.toggle {open:false}）
    hpRequest("panel.toggle", { open: false }).catch(function () { /* 插件未启用：静默 */ });
    annoPanelOpen = true;
    els.annoPanel.style.display = "flex";
    // P2 合同 §5.4：面板标题主显示 display_name（旧 DTO 回落 name）
    var panelName = slideDisplayName(state.slide) || state.slide.name || "";
    els.annoPanelTitle.textContent = t("anno.panel.title.with", {
      name: truncateMiddle(panelName, 28) });
    // 标题单行省略；悬停看完整切片名
    els.annoPanelTitle.title = panelName;
    if (annoListError) {
      // 当前切片列表从未加载成功：显式错误态 + 重试（绝不冒充「暂无标注」）
      renderAnnoPanelError();
    } else {
      renderAnnoPanel(currentAnnotations.annotations || []);
      // 之前的刷新失败过：行保留，顶部补失效提示（stale indicator）
      if (annoListStale) renderAnnoPanelStale();
    }
    updateResearchBusy();  // P3：面板打开取消稳定观察检测
  }

  function closeAnnoPanel() {
    annoPanelOpen = false;
    els.annoPanel.style.display = "none";
    updateResearchBusy();  // P3
  }

  function renderAnnoPanel(groups) {
    els.annoPanelList.innerHTML = "";
    if (!groups || groups.length === 0) {
      var empty = document.createElement("div");
      empty.className = "anno-panel-empty";
      empty.textContent = t("anno.panel.empty");
      els.annoPanelList.appendChild(empty);
      return;
    }
    groups.forEach(function (grp) {
      // 分组标题
      var gh = document.createElement("div");
      gh.className = "anno-group-head";
      gh.innerHTML = '<span class="agh-label">' + esc(grp.label) + "</span>" +
        '<span class="agh-count">' + esc(t("anno.group.count", { n: grp.count })) + "</span>";
      els.annoPanelList.appendChild(gh);

      (grp.items || []).forEach(function (it) {
        var row = document.createElement("div");
        row.className = "anno-item";
        if (!it.shared) row.classList.add("anno-private");
        var left = document.createElement("div");
        left.className = "ai-info";
        var typIcon = (it.type === "arrow") ? "↗"
          : (it.type === "polygon" ? "⬠" : (it.type === "freehand" ? "〰" : "▭"));
        var sizeStr = "";
        if ((it.type || "rect") === "rect") {
          var mmTxt = rectItemSizeText(it);
          if (mmTxt) sizeStr = " · " + mmTxt;
        }
        else if (it.type === "arrow") sizeStr = " · (" + it.x1 + "," + it.y1 + ")→(" + it.x2 + "," + it.y2 + ")";
        else if (it.type === "freehand" || it.type === "polygon") sizeStr = " · " + t("anno.free.points", { n: (it.points ? it.points.length : 0) });
        // §6.3：锁定标记在面板行内给锁徽章（工具条内同样有锁图标）
        var lockBadge = itemGeometryLocked(it)
          ? '<span class="anno-lock-badge" title="' + esc(t("anno.lock.badge.tip")) + '">🔒</span>'
          : "";
        // P1-7：私有标注用「私有」徽章表达，不再整行降透明度。
        var privateBadge = (!it.shared)
          ? '<span class="anno-private-badge">' + esc(t("anno.private.badge")) + "</span>"
          : "";
        left.innerHTML =
          '<div class="ai-title"><span class="ai-type-icon">' + typIcon + "</span>" +
          '<span class="ai-label">' + esc(grp.label) + "</span>" + lockBadge + privateBadge + sizeStr + "</div>" +
          '<div class="ai-sub">' + fmtTime(it.ts) +
          (it.token ? esc(t("anno.sub.source", { s: String(it.token).slice(0, 6) })) : "") +
          (it.visitor ? esc(t("anno.sub.visitor", { s: String(it.visitor).slice(0, 6) })) : "") + "</div>";
        row.appendChild(left);

        // 「公开」切换钮：管理员可策展任意来源标注
        var sharedBtn = document.createElement("button");
        sharedBtn.className = "ai-share" + (it.shared ? " on" : "");
        sharedBtn.textContent = it.shared ? "🌐" : "👁";
        sharedBtn.title = it.shared ? t("anno.shared.on.title")
                                    : t("anno.shared.off.title");
        sharedBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          toggleAnnoShared(it, sharedBtn, row);
        });
        row.appendChild(sharedBtn);

        // Stage 3c-1：AI 标注审核状态（接受/驳回）。仅 AI 标注（itemIsAi，
        // 与服务端 author_kind 同口径）且 pending 显示按钮；已
        // accepted/rejected 显示状态徽章。
        if (itemIsAi(it)) {
          if (it.review_status === "pending") {
            var acceptBtn = document.createElement("button");
            acceptBtn.className = "ai-op ai-review-accept";
            acceptBtn.textContent = t("anno.review.accept");
            acceptBtn.title = t("anno.review.accept.tip");
            acceptBtn.addEventListener("click", function (ev) {
              ev.stopPropagation();
              reviewAnnotation(it, "accept");
            });
            row.appendChild(acceptBtn);
            var rejectBtn = document.createElement("button");
            rejectBtn.className = "ai-op ai-review-reject";
            rejectBtn.textContent = t("anno.review.reject");
            rejectBtn.title = t("anno.review.reject.tip");
            rejectBtn.addEventListener("click", function (ev) {
              ev.stopPropagation();
              reviewAnnotation(it, "reject");
            });
            row.appendChild(rejectBtn);
          } else if (it.review_status === "accepted" || it.review_status === "rejected") {
            var revBadge = document.createElement("span");
            revBadge.className = "anno-review-badge " + it.review_status;
            revBadge.textContent = t(it.review_status === "accepted"
              ? "anno.review.accepted" : "anno.review.rejected");
            row.appendChild(revBadge);
          }
        }

        // AI 动作（P1-6）：所有带 annotation_id 的标注都挂 fork「快速问答」+ branch
        // 「从此处深读」两个小按钮（图标+短文字），不再按 source 区分。fork 轻量就地
        // 展开；branch 进 AI 面板开/续分支会话（复用既有或新建）。
        if (it.annotation_id) {
          var annoAid = it.annotation_id;
          buildAnnoAiActions(row, annoAid, "op");

          // 单条导出（ID 通道就绪时）：紧跟「加入 AI 对话」，与头部导出同图标
          if (slideIdApiOn() && state.slide && state.slide.id) {
            var exportBtn = document.createElement("button");
            exportBtn.type = "button";
            exportBtn.className = "ai-op ai-export";
            exportBtn.title = t("anno.export.one.title");
            exportBtn.setAttribute("aria-label", t("anno.export.one.title"));
            exportBtn.innerHTML =
              '<svg class="ai-act-svg" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
              '<path d="M12 4v11"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M5 19h14"/></svg>' +
              '<span class="ai-act-tx">' + esc(t("anno.export.one")) + "</span>";
            exportBtn.addEventListener("click", function (ev) {
              ev.stopPropagation();
              annoExportOne(it, grp.label, exportBtn);
            });
            row.appendChild(exportBtn);
          }
        }

        // 编辑钮：跳转到该标注并进入选中编辑态
        var editBtn = document.createElement("button");
        editBtn.className = "ai-op ai-edit";
        editBtn.textContent = "✎";
        editBtn.title = t("anno.edit.title");
        editBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          jumpAndEditAnno(it);
        });
        row.appendChild(editBtn);

        // 删除钮：调 DELETE 接口
        var delBtn = document.createElement("button");
        delBtn.className = "ai-op ai-del";
        delBtn.textContent = "🗑";
        delBtn.title = t("anno.del.title");
        delBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          deleteAnnoItem(it);
        });
        row.appendChild(delBtn);

        row.style.cursor = "pointer";
        row.addEventListener("click", function (ev) {
          // 点击落在操作按钮区（分享/AI 动作/编辑/删除）则交给按钮自身处理，不触发跳转
          if (ev.target.closest(".ai-share, .ai-op, .ai-action-chip, .fork-chat")) return;
          // §6.1：点击行 = 定位并选中（不再隐藏其它标记；「只看这条」在 ⋯ 内）。
          jumpToAnno(it);
        });
        els.annoPanelList.appendChild(row);
      });
    });
  }

  function fmtTime(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    var p = function (n) { return n < 10 ? "0" + n : n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  // 兜底：解析某标注条目在其 token 下的 index（仅旧缓存无 index 时使用；
  // annotations 接口现已直接带 index，正常路径走 resolveIndexFast 不会到这。
  // 通过 /api/share/rois 取该 token 列表，按 slide+ts+几何匹配）
  function resolveAnnoIndex(it) {
    var token = it.token;
    if (!token) return Promise.reject(new Error(t("anno.no.token")));
    // annotations 接口的条目可能不带 slide（旧数据），用当前切片名兜底
    var slideName = it.slide || (state.slide ? state.slide.name : null);
    return apiFetch("/api/share/rois")
      .then(function (r) { return r.json(); })
      .then(function (rois) {
        var cands = (rois || []).filter(function (r) { return r.token === token; });
        // 优先按 slide+ts 精确匹配；ts 不在则退回 slide+几何
        var match = null;
        for (var i = 0; i < cands.length; i++) {
          var r = cands[i];
          if (r.slide === slideName && Number(r.ts) === Number(it.ts)) { match = r; break; }
        }
        if (!match) {
          for (var j = 0; j < cands.length; j++) {
            var rr = cands[j];
            if (rr.slide !== slideName || (rr.type || "rect") !== (it.type || "rect")) continue;
            if ((rr.type || "rect") === "rect" &&
                Number(rr.x) === Number(it.x) && Number(rr.y) === Number(it.y) &&
                Number(rr.side_px) === Number(it.side_px)) { match = rr; break; }
            if (rr.type === "arrow" &&
                Number(rr.x1) === Number(it.x1) && Number(rr.y1) === Number(it.y1) &&
                Number(rr.x2) === Number(it.x2) && Number(rr.y2) === Number(it.y2)) { match = rr; break; }
            if ((rr.type === "freehand" || rr.type === "polygon") &&
                rr.points && it.points &&
                rr.points.length === it.points.length) { match = rr; break; }
          }
        }
        if (!match) throw new Error(t("anno.not.found"));
        return match.index;
      });
  }

  // 快速取 index：新数据（annotations 接口已带 index）直接用本地 it.index，
  // 省掉一次 /api/share/rois 全量拉取；仅极端旧缓存（无 index）才回退
  // resolveAnnoIndex 全量反推。
  function resolveIndexFast(it) {
    if (it && it.index != null) return Promise.resolve(it.index);
    return resolveAnnoIndex(it);
  }

  // 切换某标注的「公开」状态（策展）
  // Stage 3c-1：AI 标注审核（接受/驳回）。POST review → 刷新当前切片标注。
  // 寻址键优先 annotation_id（稳定 id 路由——index 是 token 下全体用户共享
  // 的插入序位置，他人增删后缓存的 index 会指向另一条标注）；缺
  // annotation_id 的历史行回退旧 index 路径（旧插件/旧缓存兼容）。
  function reviewAnnotation(it, action) {
    var token = it.token;
    if (!it.annotation_id && !token) {
      toast(t("anno.no.src.token"), "error");
      return;
    }
    var urlP = it.annotation_id
      ? Promise.resolve(annoIdUrl(it.annotation_id, "/review"))
      : resolveIndexFast(it).then(function (index) {
          return "/api/annotation/" + encodeURIComponent(token) + "/" +
            index + "/review";
        });
    urlP
      .then(function (url) {
        return apiFetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: action }),
        }).then(function (r) {
          if (!r.ok) return r.json().then(function (j) {
            throw new Error(j.error || (t("anno.update.fail") + " " + r.status));
          });
          return r.json();
        });
      })
      .then(function () {
        toast(t(action === "accept" ? "anno.review.accepted" : "anno.review.rejected"),
              "success");
        // P3 研究采集：AI 标注审核写入成功才记 annotation_accept/reject
        // （§7.1：来源 human_review，与 AI 自动标注不混同）
        if (researchTelemetry) {
          researchTelemetry.notifyAnnotation({
            action: action === "accept"
              ? "annotation_accept" : "annotation_reject",
            annotationId: it.annotation_id,
          });
        }
        refreshCurrentAnnotations();
      })
      .catch(function (e) { toast(e.message || t("anno.update.fail"), "error"); });
  }

  function toggleAnnoShared(it, btnEl, rowEl) {
    if (!it.annotation_id) { toast(t("anno.no.src.token"), "error"); return; }
    var target = !it.shared;
    btnEl.disabled = true;
    var slideName = it.slide || (state.slide && state.slide.name);
    apiFetch("/api/share/list")
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (shares) {
        var active = (shares || []).filter(function (sh) {
          return sh.status === "active" && Array.isArray(sh.slides)
            && sh.slides.indexOf(slideName) >= 0;
        });
        if (target && active.length === 0) {
          throw new Error(t("anno.share.need.link"));
        }
        var chain = Promise.resolve();
        active.forEach(function (sh) {
          chain = chain.then(function () {
            return apiFetch("/api/annotation/id/" + encodeURIComponent(it.annotation_id), {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                grantee_kind: "share_token",
                grantee_id: sh.token,
                revoke_grant: !target,
              }),
            }).then(function (r) {
              if (!r.ok) return r.json().then(function (j) {
                throw new Error(j.error || (t("anno.update.fail") + " " + r.status));
              });
              return r.json();
            });
          });
        });
        return chain;
      })
      .then(function () {
        it.shared = target;
        btnEl.classList.toggle("on", target);
        btnEl.textContent = target ? "🌐" : "👁";
        btnEl.title = target ? t("anno.shared.on.title")
                             : t("anno.shared.off.title");
        if (rowEl) {
          rowEl.classList.toggle("anno-private", !target);
          // P1-7：同步「私有」徽章（增/删 DOM），避免切换后徽章残留/缺失。
          var labelEl = rowEl.querySelector(".ai-title .ai-label");
          if (labelEl) {
            var old = labelEl.parentNode.querySelector(".anno-private-badge");
            if (target) {
              if (old) old.remove();
            } else if (!old) {
              var badge = document.createElement("span");
              badge.className = "anno-private-badge";
              badge.textContent = t("anno.private.badge");
              labelEl.parentNode.insertBefore(badge, labelEl.nextSibling);
            }
          }
        }
        toast(target ? t("anno.set.public") : t("anno.set.private"), "success");
      })
      .catch(function (e) { toast(t("anno.update.fail3", { e: e.message }), "error"); })
      .finally(function () { btnEl.disabled = false; });
  }

  // 点击标注条目：fitBounds（按类型算包围盒）+ 在画布上选中高亮该标注。
  // 不再画黄色临时 ROI 框（旧实现会残留且对箭头/描图显示 "0mm × 0mm"，
  // 还会覆盖 state.roi 破坏 ROI 模式选区）。改为复用既有的"选中态高亮"：
  // 被 editItem 选中的标注在 redrawAnnoCanvas/drawAnnoItem 中以蓝色描边。
  function jumpToAnno(it) {
    if (!state.slide || !viewer || !viewer.viewport) return;
    var typ = it.type || "rect";
    var x, y, side;
    if (typ === "arrow") {
      x = Math.min(it.x1, it.x2); y = Math.min(it.y1, it.y2);
      side = Math.max(Math.abs(it.x2 - it.x1), Math.abs(it.y2 - it.y1));
      side = Math.max(side, 1);
    } else if (typ === "freehand" || typ === "polygon") {
      var xs = it.points.map(function (p) { return p[0]; });
      var ys = it.points.map(function (p) { return p[1]; });
      x = Math.min.apply(null, xs); y = Math.min.apply(null, ys);
      side = Math.max(Math.max.apply(null, xs) - x, Math.max.apply(null, ys) - y);
      side = Math.max(side, 1);
    } else {
      // 升级 C：按真实 w/h 包围（不重新正方形化）
      x = it.x; y = it.y;
      side = Math.max(rectItemW(it), rectItemH(it));
    }
    // 扩 20% 边距
    var pad = side * 0.2;
    try {
      var rect = viewer.viewport.imageToViewportRectangle(
        x - pad, y - pad, side + pad * 2, side + pad * 2);
      viewer.viewport.fitBounds(rect);
    } catch (e) {}

    // 选中高亮：flatItems 是 rebuildFlatItems 生成的副本，it 来自面板分组，
    // 引用不同，需按 token+ts+type 在 flatAnnoItems() 里找到匹配副本再选中。
    if (!state.showAnno) {
      state.showAnno = true;
      syncAnnoAllBtns();
    }
    var match = null;
    var items = flatAnnoItems();
    for (var i = 0; i < items.length; i++) {
      var f = items[i];
      if (f.token === it.token && Number(f.ts) === Number(it.ts) &&
          (f.type || "rect") === (it.type || "rect")) { match = f; break; }
    }
    if (match) {
      editItem = match;     // 选中态：drawAnnoItem 会给蓝色描边
      // §6.1：定位 ≠ 只看——选中不再隐藏其它标记（focus 只由 ⋯「只看此标记」控制）
      editing = false;      // 备注卡查看形态（选中本身即可拖柄，§6.2）
      closeEditCard();      // 不弹编辑卡（仅点击行，非"编辑"按钮）
      openAnnoToolbar();
      redrawAnnoCanvas();
    }
    return match;           // 供 jumpAndEditAnno 复用匹配结果
  }

  // ---------- 编辑卡（标注面板顶部） + 删除 ----------
  // 显式编辑态：非编辑态只显示「✎ 编辑」入口，点它才进入可拖动编辑态；
  // 备注 textarea 两种状态下都可直接改（备注改动不属于"移动"）。
  function openEditCard(it) {
    var wrap = $("anno-edit-wrap");
    if (!wrap) return;
    var typ = it.type || "rect";
    var titleText =
      typ === "arrow" ? t("edit.title.arrow") :
      typ === "polygon" ? t("edit.title.polygon") :
      (typ === "freehand" ? t("edit.title.free") : t("edit.title.rect"));
    if (itemGeometryLocked(it)) titleText += " 🔒";
    wrap.innerHTML = "";
    var card = document.createElement("div");
    card.className = "anno-edit-card";
    var head = document.createElement("div");
    head.className = "aec-head";
    head.textContent = titleText;
    card.appendChild(head);
    var ta = document.createElement("textarea");
    ta.className = "aec-note";
    ta.maxLength = 500;
    ta.placeholder = t("edit.note.ph");
    ta.value = it.note || "";
    ta.rows = 2;
    card.appendChild(ta);
    var ops = document.createElement("div");
    ops.className = "aec-ops";
    if (editing) {
      // 编辑态：保存 / 取消 / 删除
      var saveB = document.createElement("button");
      saveB.className = "btn primary small"; saveB.textContent = t("edit.save");
      var cancelB = document.createElement("button");
      cancelB.className = "btn secondary small"; cancelB.textContent = t("edit.cancel");
      var delB = document.createElement("button");
      delB.className = "btn danger small"; delB.textContent = t("edit.del");
      ops.appendChild(delB); ops.appendChild(cancelB); ops.appendChild(saveB);
      card.appendChild(ops);
      wrap.appendChild(card);
      wrap.style.display = "block";

      saveB.addEventListener("click", function () { commitAdminEdit(it, ta.value); });
      cancelB.addEventListener("click", function () { cancelAdminEdit(it); });
      delB.addEventListener("click", function () {
        delB.disabled = true;
        deleteAnnoItem(it);
      });
      ta.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); commitAdminEdit(it, ta.value); }
      });
    } else {
      // 非编辑态：✎ 编辑 / 保存 / 删除
      var editB = document.createElement("button");
      editB.className = "btn small"; editB.textContent = t("edit.enter");
      editB.title = t("edit.enter.title");
      var saveB2 = document.createElement("button");
      saveB2.className = "btn primary small"; saveB2.textContent = t("edit.save");
      var delB2 = document.createElement("button");
      delB2.className = "btn danger small"; delB2.textContent = t("edit.del");
      ops.appendChild(delB2); ops.appendChild(editB); ops.appendChild(saveB2);
      card.appendChild(ops);
      wrap.appendChild(card);
      wrap.style.display = "block";

      editB.addEventListener("click", function () {
        editing = true;
        redrawAnnoCanvas();
        openEditCard(it);
        updateResearchBusy();  // P3：编辑卡打开取消稳定观察检测
      });
      saveB2.addEventListener("click", function () { commitAdminEdit(it, ta.value); });
      delB2.addEventListener("click", function () {
        delB2.disabled = true;
        deleteAnnoItem(it);
      });
      ta.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); commitAdminEdit(it, ta.value); }
      });
    }
  }

  function closeEditCard() {
    var wrap = $("anno-edit-wrap");
    if (wrap) { wrap.innerHTML = ""; wrap.style.display = "none"; }
  }

  // 收集编辑后几何（图片坐标，round 整数，clamp ≥0）。升级 C：rect 以成对
  // w/h 提交（v2 契约），附带 expected_revision（CAS，§6.1）。
  // §5.4：polygon 保留原类型（points 序列化），不改写成 freehand。
  function buildEditGeom(it) {
    var typ = it.type || "rect";
    var g = {};
    if (typ === "rect") {
      g.x = Math.max(0, Math.round(it.x));
      g.y = Math.max(0, Math.round(it.y));
      g.w = clamp(Math.round(rectItemW(it)), 1, RECT_MAX_SIDE_PX);
      g.h = clamp(Math.round(rectItemH(it)), 1, RECT_MAX_SIDE_PX);
    } else if (typ === "arrow") {
      g.x1 = Math.max(0, Math.round(it.x1));
      g.y1 = Math.max(0, Math.round(it.y1));
      g.x2 = Math.max(0, Math.round(it.x2));
      g.y2 = Math.max(0, Math.round(it.y2));
    } else if (typ === "freehand" || typ === "polygon") {
      g.points = (it.points || []).map(function (p) {
        return [Math.max(0, Math.round(p[0])), Math.max(0, Math.round(p[1]))];
      });
    }
    return g;
  }

  // 提交管理员编辑（§6.3 差量更新）：几何没变就不带 geom（只 PATCH note），
  // 否则锁定后连改备注都会被当成移动拒绝。新 UI 始终带 expected_revision。
  // 409 按错误码区分：annotation_locked ≠ revision_conflict（§6.4）。
  function commitAdminEdit(it, noteVal) {
    if (!it || !it.annotation_id) {
      toast(t("save.fail"), "error");
      return;
    }
    var geom = buildEditGeom(it);
    var before = editBeforeSnapshot;
    var geomChanged = !before || !geomEqual(before.geom, geom);
    var body = { note: noteVal };
    if (geomChanged) body.geom = geom;
    if (Number(it.revision) > 0) body.expected_revision = Number(it.revision);
    // rect 的 size_mm 前端重算（仅真正方形的兼容展示字段；v2 附属信息）
    if (geomChanged && (it.type || "rect") === "rect" && geom.w === geom.h &&
        state.mppX && state.mppX > 0) {
      body.geom.size_mm = Math.round(geom.w * state.mppX / 1000 * 100) / 100;
    }
    apiFetch("/api/annotation/id/" + encodeURIComponent(it.annotation_id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (r.status === 409) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          var err = new Error(j.error || "revision_conflict");
          err.conflict = true;
          err.locked = (j.error === "annotation_locked");
          // §6.4：读回服务端版本（契约 409 带 current 条目；旧后端回落
          // current_revision），保留本地草稿供比较，不静默覆盖
          err.serverEntry = j.current || null;
          err.currentRevision = (j.current && j.current.revision != null)
            ? j.current.revision : j.current_revision;
          throw err;
        });
      }
      if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || t("save.fail")); });
      return r.json();
    })
      .then(function (j) {
        toast(t("edit.saved"), "success");
        // P3 研究采集：编辑业务写入成功才记 annotation_update（§7.1）
        if (researchTelemetry) {
          researchTelemetry.notifyAnnotation({
            action: "annotation_update",
            shapeType: it.type || "rect",
            geom: geomChanged ? geom : null,
            annotationId: it.annotation_id,
          });
        }
        var snap = editBeforeSnapshot;
        if (snap && state.slide && (snap.annotationId || it.annotation_id)) {
          pushUndoEntry({
            kind: "edit",
            slide: state.slide.name,
            slide_id: state.slide.id || null,
            userId: currentUserId,
            annotationId: snap.annotationId || it.annotation_id,
            revision: (j && j.revision != null) ? j.revision : it.revision,
            before: { geom: snap.geom, note: snap.note },
            after: { geom: geomChanged ? geom : snap.geom, note: noteVal },
          });
        }
        editItem = null;
        editing = false;
        state.polygonVertexEdit = null;
        editBeforeSnapshot = null;
        closeAnnoToolbar();
        closeEditCard();
        refreshCurrentAnnotations();
        loadAnnotationsIndex().then(function () {
          renderFolderBrowser();
        });
      })
      .catch(function (e) {
        if (e && e.conflict && e.locked) {
          // annotation_locked：结束编辑手势并说明原因（不强行补发）
          toast(t("anno.error.locked", {
            rev: e.currentRevision != null ? e.currentRevision : "?",
          }), "error");
          editItem = null;
          editing = false;
          state.polygonVertexEdit = null;
          editBeforeSnapshot = null;
          closeAnnoToolbar();
          closeEditCard();
          refreshCurrentAnnotations();
        } else if (e && e.conflict) {
          // revision_conflict：保留本地修改草稿（编辑卡与本地几何不动），
          // 读回的服务端 revision 作为下一次保存的 CAS 基准
          if (e.currentRevision != null && editItem) it.revision = e.currentRevision;
          if (e.serverEntry && e.serverEntry.geometry_locked && editItem) {
            it.geometry_locked = true;
          }
          toast(t("edit.conflict.keep", {
            rev: e.currentRevision != null ? e.currentRevision : "?",
          }), "error");
        } else {
          toast(t("save.fail2", { e: e.message }), "error");
        }
      });
  }

  function cancelAdminEdit(it) {
    editItem = null;
    editing = false;
    closeEditCard();
    refreshCurrentAnnotations();
  }

  // 删除标注（管理员，任意来源）：
  // 寻址键优先 annotation_id（稳定 id 路由 DELETE /api/annotation/id/<id>）。
  // index 是该 token 下按插入序的位置（工作台全体用户共享同一列表）：数据
  // 变动后本地缓存 index（it.index）可能指向另一条标注——非 owner 403「只能
  // 修改自己创建的标注」（dogfood BUG-1 2026-10-17：连删 3 个自己的标记，
  // 第 3 个 403），owner 甚至静默删错任何人的。按 id 删除不受列表变动影响：
  //   1) 带 annotation_id（现役数据一律有）→ id 路由；
  //   2) 404 → 该标注服务端已不存在，删除幂等，视为成功（乐观移除 + toast）；
  //   3) 409 annotation_locked → 锁定对象说明原因并刷新恢复；
  //   4) 其他错误（网络/403 等）toast「删除失败」并刷新恢复；
  //   5) 仅缺 annotation_id 的历史行回退旧 index 路径（幂等 + 过期重试）：
  //      先按 resolveIndexFast（优先 it.index）发 DELETE；404 时用
  //      resolveAnnoIndex 反推最新 index 重试一次；仍 404 或反推不到 →
  //      视为已不存在（幂等成功）。
  function deleteAnnoItem(it) {
    // 发 DELETE，返回 { ok, status }：成功 ok=true；失败携带 HTTP status 供上层
    // 区分 404（幂等可放过）与其他错误（需报错恢复）。
    function sendDelete(index) {
      return apiFetch("/api/annotation/" + encodeURIComponent(it.token) + "/" + index, {
        method: "DELETE",
      }).then(function (r) {
        if (r.ok) return { ok: true, status: r.status };
        // 消费 body 并取错误码（§6.3：锁定对象 DELETE → 409 annotation_locked）
        return r.json().catch(function () { return {}; }).then(function (j) {
          return { ok: false, status: r.status, errorCode: j && j.error };
        });
      });
    }

    // by-id DELETE：寻址不依赖 index（列表增删安全）
    function sendDeleteById() {
      return apiFetch(annoIdUrl(it.annotation_id), { method: "DELETE" })
        .then(function (r) {
          if (r.ok) return { ok: true, status: r.status };
          return r.json().catch(function () { return {}; }).then(function (j) {
            return { ok: false, status: r.status, errorCode: j && j.error };
          });
        });
    }

    // 乐观更新：成功路径与"视为已删除"路径共用，立即反馈 + 后台异步同步。
    function applyAnnoRemoved() {
      // 1) flatItems 按引用移除（画布数据源）
      var items = flatAnnoItems();
      var fi = items.indexOf(it);
      if (fi >= 0) items.splice(fi, 1);
      // 2) currentAnnotations 分组中按引用移除，grp.count--，空组剔除
      if (currentAnnotations && currentAnnotations.annotations) {
        var groups = currentAnnotations.annotations;
        for (var gi = groups.length - 1; gi >= 0; gi--) {
          var g = groups[gi];
          var ii = (g.items || []).indexOf(it);
          if (ii >= 0) {
            g.items.splice(ii, 1);
            g.count = Math.max(0, (g.count || 1) - 1);
            if (g.items.length === 0) groups.splice(gi, 1);
          }
        }
      }
      // 3) 若当前编辑/选中项正是它，清选中并关编辑卡
      //    （editItem 多为 flatItems 副本，引用不等时按 token+ts+type 判定）
      if (editItem && (editItem === it ||
          (editItem.token === it.token && Number(editItem.ts) === Number(it.ts) &&
           (editItem.type || "rect") === (it.type || "rect")))) {
        editItem = null;
        editing = false;
        closeEditCard();
      }
      // focusAnno 若指向被删项（按引用或 token+ts+type 判定）→ 清空恢复显示全部
      if (state.focusAnno && (state.focusAnno === it ||
          (state.focusAnno.token === it.token && Number(state.focusAnno.ts) === Number(it.ts) &&
           (state.focusAnno.type || "rect") === (it.type || "rect")))) {
        state.focusAnno = null;
      }
      // 4) 重建扁平缓存 + 重绘 + 面板即时重渲 + 立即 toast
      rebuildFlatItems();
      redrawAnnoCanvas();
      if (annoPanelOpen) renderAnnoPanel((currentAnnotations || {}).annotations || []);
      toast(t("del.anno.done"), "success");
      // ---- 后台异步同步（不阻塞上面的即时反馈）----
      refreshCurrentAnnotations();
      // 全量索引只影响项目/未归类行的计数徽章，后台慢慢同步即可
      loadAnnotationsIndex().then(function () {
        renderFolderBrowser();
      });
    }

    var chain = it.annotation_id
      ? sendDeleteById().then(function (res) {
          if (res.ok) return { treated: true };
          // §6.3：锁定对象的删除被服务端 409 annotation_locked 拒绝——
          // 不当幂等成功，说明原因并恢复服务端状态
          if (res.status === 409 && res.errorCode === "annotation_locked") {
            toast(t("anno.lock.delete.denied"), "error");
            return { blocked: true };
          }
          // 404：该标注服务端已不存在，删除幂等，视为成功
          if (res.status === 404) return { treated: true, alreadyGone: true };
          throw new Error(t("del.fail") + " (" + res.status + ")");
        })
      : resolveIndexFast(it)
          .then(function (index) {
            return sendDelete(index).then(function (res) {
              if (res.ok) return { treated: true };
              // §6.3：锁定对象的删除被服务端 409 annotation_locked 拒绝——
              // 不当幂等成功，说明原因并恢复服务端状态
              if (res.status === 409 && res.errorCode === "annotation_locked") {
                toast(t("anno.lock.delete.denied"), "error");
                return { blocked: true };
              }
              // 第一次 404：index 可能过期，用 resolveAnnoIndex 反推最新 index 重试一次
              if (res.status === 404) {
                return resolveAnnoIndex(it)
                  .then(function (freshIndex) { return sendDelete(freshIndex); })
                  .then(function (res2) {
                    if (res2.ok) return { treated: true };
                    // 重试仍 404 → 服务端已无此标注，删除幂等，视为成功
                    if (res2.status === 404) return { treated: true, alreadyGone: true };
                    // 其他错误冒泡到 catch
                    throw new Error(t("del.fail") + " (" + res2.status + ")");
                  })
                  .catch(function (e) {
                    // resolveAnnoIndex 抛"未找到对应标注"/"Annotation not found" → 服务端已无此标注，视为成功
                    if (e && /未找到对应标注|Annotation not found|not found/i.test(e.message)) {
                      return { treated: true, alreadyGone: true };
                    }
                    throw e; // 其余错误继续冒泡
                  });
              }
              // 非 404 错误：报错并在 catch 中刷新恢复
              throw new Error(t("del.fail") + " (" + res.status + ")");
            });
          });
    chain
      .then(function (outcome) {
        // 成功或"已不存在视为成功"，统一走乐观移除；锁定拒绝只刷新恢复
        if (outcome && outcome.blocked) {
          refreshCurrentAnnotations();
          return;
        }
        applyAnnoRemoved();
      })
      .catch(function (e) {
        toast(t("del.fail2", { e: (e && e.message ? e.message : t("del.unknown")) }), "error");
        // 失败恢复：重新拉取真实状态
        refreshCurrentAnnotations();
      });
  }

  // 跳转并打开编辑卡（标注面板"编辑"按钮）：
  // 复用 jumpToAnno 的定位 + 选中高亮，再对匹配项打开编辑卡。
  function jumpAndEditAnno(it) {
    var match = jumpToAnno(it);
    if (match) {
      editing = false;        // 打开"查看态"编辑卡（✎ 切保存/取消形态）
      openEditCard(match);
    }
  }

  // ---------- 删除切片 ----------
  // P2：删除仍按名（DELETE /api/slide/<name> 是旧路由；ID 删除 P5 才出——
  // 本阶段不动，sname 来自列表行的 legacy 名）
  // label：确认/提示用显示名；slideId：权威身份（有则走 ID 端点）；
  // legacyName：仅无 ID 时的旧名端点键
  function deleteSlide(label, slideId, legacyName) {
    var name = label;
    if (!confirm(t("del.slide.confirm", { name: name }))) return;
    var byId = !!slideId && slideIdApiOn();
    if (!byId && !legacyName) {
      toast(t("del.slide.fail", { e: t("slide.not.found") }), "error");
      return;
    }
    var url = byId ? "/api/slides/" + encodeURIComponent(slideId)
                   : "/api/slide/" + encodeURIComponent(legacyName);
    apiFetch(url, { method: "DELETE" })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.error); });
        if (state.slide && (byId ? state.slide.id === slideId
                                 : state.slide.name === legacyName)) {
          // 与临时到期共用同一无切片基线（空态卡回归 + 顶栏上下文复位）
          enterNoSlideUiState();
        }
        toast(t("del.slide.done", { name: name }), "success");
        loadAll();
      })
      .catch(function (e) { toast(t("del.slide.fail", { e: e.message }), "error"); });
  }


  // ---------- 多文件独立进度行（U3 §3.5：修共用进度条的 bug） ----------
  // 每个上传文件一行（名称 + 独立进度条 + 三段状态文本）；行挂在侧栏
  // #upload-progress-list 容器下，完成后短暂保留再移除。容器缺失（旧模板）
  // 时回退到旧的单进度条元素。
  // 用户五步口径（2026-10-20 复核）：确有真实分母的两个阶段（正在上传 /
  // 服务器接收 checkpoint）把百分比并进**主阶段标签**——「正在上传（42%）」
  // 「服务器正在处理（42%）」（zh 全角括号、en 半角，形由 upload.stage.pct
  // 按语言给出）；其余阶段百分比留在次行，done/终态绝不带百分比。
  var PCT_IN_LABEL_KEYS = {
    "upload.cos.stage.uploading": 1,
    "upload.cos.stage.downloading": 1,
  };
  function humanSize(n) {
    if (n >= 1024 * 1024 * 1024) return (n / (1024 * 1024 * 1024)).toFixed(1) + " GB";
    if (n >= 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
  }

  function makeUploadRow(file) {
    var host = els.uploadProgressList;
    var row = document.createElement("div");
    var nameEl = document.createElement("div");
    var barWrap = document.createElement("div");
    var bar = document.createElement("div");
    var statusEl = document.createElement("div");
    var bytesEl = document.createElement("div");
    row.className = "upload-item";
    nameEl.className = "upload-item-name";
    barWrap.className = "upload-item-bar";
    bar.className = "upload-item-bar-fill";
    statusEl.className = "upload-item-status";
    bytesEl.className = "upload-item-bytes";
    // 阶段文本走 polite 播报（role=status），但只装阶段名——百分比/字节在
    // 播报节流意义上属于逐事件抖动，写进独立的非播报元素（U1 §2.3：
    // aria-live 只在阶段变化时播报，不要每一个网络事件都播报）
    if (statusEl.setAttribute) statusEl.setAttribute("role", "status");
    // 进度条本体具备 progressbar 语义（aria-valuenow 由 setStage 维护；
    // 无 valuenow 即不定态）
    if (barWrap.setAttribute) {
      barWrap.setAttribute("role", "progressbar");
      barWrap.setAttribute("aria-valuemin", "0");
      barWrap.setAttribute("aria-valuemax", "100");
    }
    nameEl.textContent = (file && file.name || "?") + " · " +
      humanSize((file && file.size) || 0);
    barWrap.appendChild(bar);
    row.appendChild(nameEl);
    row.appendChild(barWrap);
    row.appendChild(statusEl);
    row.appendChild(bytesEl);
    var fallback = !host || !host.appendChild;
    if (!fallback) host.appendChild(row);
    var removed = false;
    function removeLater(delay) {
      if (removed) return;
      removed = true;
      setTimeout(function () {
        if (row.parentNode && row.parentNode.removeChild) {
          row.parentNode.removeChild(row);
        }
      }, delay);
    }
    return {
      // 三段状态：正在传输（字节事件为准）→ 服务端校验 → 上传成功
      // note（可选，COS 直传引入）：字节文本（已传输 X / 总大小 Y、
      // 「数据已发送，等待确认」）——写进独立的非播报元素（次行细节）；
      // indeterminate（U1）：当前阶段无量化信息 → 不定态活动指示
      // （移除 aria-valuenow、清内联宽度让 CSS 动画接管），
      // 百分比只表示当前阶段，绝不合成为全流程百分比
      setStage: function (stageKey, frac, note, indeterminate) {
        var label = tt(stageKey);
        var pct = (!indeterminate && frac !== undefined && frac !== null &&
                   isFinite(frac))
          ? Math.max(0, Math.min(100, Math.round(frac * 100))) : null;
        var inLabel = pct !== null && PCT_IN_LABEL_KEYS[stageKey];
        var pctText = inLabel ? tt("upload.stage.pct", { pct: pct }) : "";
        statusEl.textContent = label + pctText;
        bytesEl.textContent = inLabel
          ? (note || "")
          : ((pct !== null ? pct + "% · " : "") + (note || ""));
        if (bar.classList) {
          if (indeterminate) bar.classList.add("upload-item-bar-indeterminate");
          else bar.classList.remove("upload-item-bar-indeterminate");
        }
        if (barWrap.setAttribute) {
          if (pct !== null) barWrap.setAttribute("aria-valuenow", String(pct));
          else if (barWrap.removeAttribute) barWrap.removeAttribute("aria-valuenow");
        }
        if (fallback) {
          // 旧模板回退：写入共用进度条（单文件场景行为与旧版一致）
          if (els.progressWrap && els.progressWrap.style) els.progressWrap.style.display = "block";
          if (els.progressBar && els.progressBar.style) els.progressBar.style.width = (pct === null ? 0 : pct) + "%";
          if (els.progressText) els.progressText.textContent =
            label + pctText + (note ? " · " + note : "");
        } else if (bar.style) {
          // 不定态清内联宽度：否则旧宽度覆盖动画类的 30%（条不动）
          bar.style.width = pct === null ? "" : pct + "%";
        }
      },
      markError: function () {
        if (row.classList) row.classList.add("upload-item-error");
        if (bar && bar.classList) {
          bar.classList.add("upload-item-bar-error");
          bar.classList.remove("upload-item-bar-indeterminate");
        }
      },
      finish: function (keepMs) {
        if (fallback) {
          if (els.progressWrap && els.progressWrap.style) els.progressWrap.style.display = "none";
        } else {
          removeLater(keepMs === undefined ? 6000 : keepMs);
        }
      },
      _row: row,
      _bar: bar,
      _status: statusEl,
      _bytes: bytesEl,
    };
  }


  function uploadedTarget(body, file) {
    var b = body || {};
    var sid = b.slide_id || null;
    // 展示/回落名：沿用旧逻辑口径（ready+canonical_name 优先，再响应名/slide，
    // 最后本地文件名）
    var name = ((b.state === "ready" && b.canonical_name) || b.name ||
                b.slide || b.canonical_name) ||
               ((file && file.name) || "");
    if (!sid) {
      console.debug(
        "[slide-id] 上传响应缺 slide_id，按名回落打开（旧后端兼容，P3 拆除）:",
        name);
    }
    return { id: sid, name: name };
  }


  // snap（folder-experience §1.3）：可选目标快照——有就传给关联（冻结语义）；
  // 旧调用方（HP_UPLOAD.pollConversionJob 三参）沿用当前导入会话状态。
  function pollConversionJob(body, row, file, snap) {
    var jobId = body.conversion_job_id;
    var canonical = body.canonical_name;
    row.setStage("upload.stage.converting");
    var started = Date.now();
    var OBSERVE_NOTE_MS = 15 * 60 * 1000;   // 仅切换提示文案，不停轮询
    var notedStillProcessing = false;
    var stopped = false;
    var delay = 2000;                       // 退避：2s 起步，上限 15s
    function schedule() {
      if (stopped) return;
      delay = Math.min(delay * 2, 15000);
      setTimeout(tick, delay);
    }
    function tick() {
      if (stopped) return;
      if (!notedStillProcessing && Date.now() - started > OBSERVE_NOTE_MS) {
        notedStillProcessing = true;
        row.setStage("imp.conv.still.processing");  // 「仍在后台处理」（不是失败）
      }
      apiFetch("/api/conversions/" + encodeURIComponent(jobId))
        .then(function (r) {
          return r.json().then(function (j) {
            return { ok: r.ok, status: r.status, body: j };
          }, function () {
            return { ok: r.ok, status: r.status, body: null };  // body 非 JSON
          });
        })
        .then(function (res) {
          if (stopped) return;
          if (res.status === 401 || res.status === 403) {
            stopped = true;
            row.setStage("imp.conv.auth.stop");
            row.finish(10000);
            return;
          }
          if (res.status === 404) {
            stopped = true;
            row.setStage("imp.conv.missing");
            row.finish(10000);
            return;
          }
          if (!res.ok) {
            row.setStage("imp.conv.progress.unavailable");
            schedule();
            return;
          }
          var st = res.body && res.body.state;
          if (st === "ready") {
            var name = (res.body.canonical_name || canonical);
            var target = uploadedTarget(res.body, file);
            row.setStage("upload.stage.done");
            row.finish();
            toast(t("upload.done", { name: name }), "success");
            // 列表刷新完成后再解析打开目标：响应带 slide_id 直接用；否则按
            // canonical 名在刷新后的 /api/slides 里解析 slide_id（ID 通道不
            // 按名猜资产——解析不到才回落名，见 uploadedTarget）
            loadAll().then(function () {
              var ref = target.id;
              if (!ref) {
                var s = findSlideInfo(name);
                ref = (s && s.slide_id) || null;
              }
              if (!ref) {
                console.debug(
                  "[slide-id] 转换 ready 无 slide_id（响应与列表均未解析到），按名回落打开（旧后端兼容）:",
                  name);
                ref = name;
              }
              importAssociateUploaded(ref, name, snap || null, row);   // W4：上传行自身的目标关联（若有；§1.3 冻结快照）
              openSlide(ref);
            });
            return;
          }
          if (st === "failed" || st === "cancelled") {
            row.markError();
            row.setStage("upload.stage.failed");
            row.finish(10000);
            toast(t("upload.fail", { e: t("upload.err.conversion") }), "error");
            return;
          }
          schedule();
        })
        .catch(function () {
          if (stopped) return;
          row.setStage("imp.conv.progress.unavailable");
          schedule();
        });
    }
    tick();
  }

  function uploadFile(file, opts) {
    if (!file) return;
    opts = opts || {};
    // folder-experience §1.3：上传开始即冻结目标快照——之后侧栏切换目录、
    // 抽屉重开/下拉改动不影响本次任务的关联归属（含失败重试的目标与幂等键）
    // 面板内转换（设计 §3.5 第 1 点）：快照必须在**入队之前**取——这条既
    // 有语义与源码级断言（viewer-folders.test.ts）都由本次接线保持。
    opts.assocTarget = importTargetSnapshot();
    if (!inlineEnabled()) {          // 开关关闭 → 回退路径（控制流不变）
      legacyUploadFile(file, opts);
      return;
    }
    // 面板内转换（设计 §2.2①）：全部交给面板队列（含散文件 bundle 的
    // 「需要完整包」类型化说明，§4.1.1）。运行器懒加载；加载失败 → 回退
    // 到既有分流（绝不静默丢弃用户的选择）。opts.fileHandle：drop 事件内
    // 同步捕获的 FileSystemFileHandle Promise（MRXS 协助流程的 startIn 定位）
    ensureInlineOrch().then(function (orch) {
      if (orch) {
        orch.enqueue(file, {
          assocTarget: opts.assocTarget,
          fileHandle: opts.fileHandle || null,
        });
      }
      else legacyUploadFile(file, opts);
    });
  }

  /// 既有分流路径（开关关闭 / 面板初始化失败时的回退；控制流不变）。
  function legacyUploadFile(file, opts) {
    opts = opts || {};
    // 先转换后上传阶段 1（docs/slide-tools/upload-convert-first-phase1.md）：
    // 先按文件头嗅探直传类别（slide-sniff.js 共享模块，只读 ≤128KB 头），
    // 按类别分流——直传类（OME-TIFF/转换器 BigTIFF）与暂时直传类立即上传
    // （创建 ingestion 携带 direct_class 声明）；需要本机转换的（KFB/KFBF/
    // JPEG 编码 SVS/MRXS）走弹窗交接；未登记扩展名明确报错。嗅探失败按
    // 既有词表路径降级（服务端创建闸与 worker 核验兜底）。
    var row = makeUploadRow(file);
    var sniff = (window.HP_SLIDE_SNIFF &&
                 typeof window.HP_SLIDE_SNIFF.classifyFile === "function")
      ? window.HP_SLIDE_SNIFF.classifyFile(file)
      : Promise.resolve(null);
    sniff.then(function (cls) {
      uploadFileClassified(file, row, opts, cls);
    }, function () {
      uploadFileClassified(file, row, opts, null);
    });
  }

  function uploadFileClassified(file, row, opts, cls) {
    // U3（统一 COS，docs/cos-only-upload-agent-plan-20260928.md §4）：新任务
    // 只建 ingestion。能力不可用（capability off / 容量配置门禁未过）→ 禁用
    // 创建并提示，**不回退 V1/V2**；词表与上限以服务端 capability 为唯一
    // 权威（cosUploadEligible 判定），不合格即说明原因。
    if (!COS_UPLOAD_CONFIG) {
      row.markError();
      row.setStage("upload.cos.unavailable");
      row.finish(10000);
      return;
    }
    // 需要本机转换（KFB/KFBF/JPEG SVS/MRXS 成员）→ 面板内转换；面板不可用
    //（开关关闭/引擎初始化失败）时行内说明 + 重试，绝不跳转换器页面
    var rasterFallback = !!(cls && cls.cls === "convert" &&
                            canDirectUploadRaster(file));
    if (cls && cls.cls === "convert" && !rasterFallback) {
      convertNeededUnavailable(file, file && file.name,
        function () { uploadFile(file, opts); }, row);
      return;
    }
    if (cls && cls.cls === "unsupported") {
      row.markError();
      row.setStage("upload.stage.failed");
      // 未登记扩展名：说明 + 同行的「申请支持该格式」（设计 §7.1）。文案不再
      // 点名 /tools/slides（该分支与开关无关，开启时也不走工具页）。
      addRowButton(row, tt("upload.inline.action.requestSupport"), function () {
        openSupportForEntry({ name: file && file.name, file: file,
          error: { code: "unsupported_ext" } });
      });
      row.finish(10000);
      toast(t("upload.fail", { e: tt("upload.err.unsupported") }), "error");
      return;
    }
    if (!cosUploadEligible(file)) {
      // 服务端受理清单外但属 browser_convert 清单 → 面板内转换（嗅探不可用
      // 时的兜底）；面板不可用 → 行内说明 + 重试，绝不跳转换器页面
      if (isBrowserConvertFile(file)) {
        convertNeededUnavailable(file, file && file.name,
          function () { uploadFile(file, opts); }, row);
        return;
      }
      row.markError();
      row.setStage("upload.stage.failed");
      row.finish(10000);
      toast(t("upload.fail", { e: cosIneligibleReason(file) }), "error");
      return;
    }
    uploadFileCos(file, row, {
      resumeJobId: opts.cosRetry || null,
      skipConfirm: !!opts.cosRetry,
      // 直传类携带 direct_class 声明（ome-tiff/converter-bigtiff/
      // legacy-direct/unconverted-variant:svs-jp2k；服务端词表校验 + worker
      // 头级核验）
      directClass: rasterFallback ? "legacy-direct" :
        (cls ? (cls.directClass || null) : null),
      // 复核（设计 §3.5 / 第四轮复审 A1）：**必须把冻结的目标快照转发下去**。
      // 此前这里的字面量不含该键，uploadFileCos 内的
      // `fbUpAssocSave(jobId, opts.assocTarget)`（:10789 区）被其守卫吞掉、
      // 完成时 `cosUploadSucceeded(..., opts.assocTarget)` 收到 undefined →
      // 关联回落到「完成时刻的当前文件夹」（folder-experience §1.3 明令避免）。
      assocTarget: opts.assocTarget || null,
    });
  }

  // =========================================================================
  // 2026-10-20 用户决定：独立转换器（/tools/slides）只做本地转换与保存；
  // 工作台上传统一在工作台上传面板完成（inline-convert）。旧的「在本机转换
  // 并上传」弹窗交接（File/文件夹 postMessage、目标项目、自动上传意图）整体
  // 移除：需要转换的文件在面板不可用时行内说明 + 重试/支持，绝不打开转换器
  // 页面继续上传。
  // =========================================================================

  /// 转换器页面（仍保留本地转换与保存）；工作台只保留对它的普通页面链接
  /// 语义（saveLocal），不携带文件/意图。
  var TOOLS_PAGE_URL = "/tools/slides";

  /// bootstrap 能力下发的 browser_convert 清单（cos_upload.browser_convert.
  /// formats；服务端唯一权威——前端不自维护第二份）。嗅探不可用时的兜底
  /// 分流（uploadFileClassified）用。
  function browserConvertFormats() {
    try {
      var caps = window.HP_APP_BOOTSTRAP && window.HP_APP_BOOTSTRAP.capabilities;
      var bc = caps && caps.cos_upload && caps.cos_upload.browser_convert;
      return (bc && Array.isArray(bc.formats)) ? bc.formats : [];
    } catch (e) {
      return [];
    }
  }

  function isBrowserConvertFile(file) {
    if (!file || typeof file.name !== "string") return false;
    var ext = file.name.slice(file.name.lastIndexOf(".") + 1).toLowerCase();
    return browserConvertFormats().indexOf(ext) >= 0;
  }

  /// 需要本机转换的文件在面板内转换不可用时的**唯一**收口：行内说明 +
  /// 「重试」（重新走 uploadFile → 面板初始化/分类）+「申请支持该格式」。
  /// retry 闭包带原始输入（单文件 File 或文件夹成员行），重试不丢用户选择。
  /// 已有行（单文件 uploadFile 分流）就地复用；无行（MRXS 文件夹）新建。
  function convertNeededUnavailable(file, displayName, retry, existingRow) {
    var row = existingRow || makeUploadRow({
      name: displayName || (file && file.name) || "?",
      size: (file && file.size) || 0,
    });
    row.setStage("upload.kfb.unavailable");
    addRowButton(row, tt("upload.cos.retry"), retry);
    addRowButton(row, tt("upload.inline.action.requestSupport"), function () {
      openSupportForEntry({ name: displayName || (file && file.name) || "", file: file,
        error: { code: "needs_conversion" } });
    });
    row.finish(60000);
  }

  var COS_JOBS_KEY = "pt.cos.jobs";

  function resolveCosConfig() {
    // C4：校验/规范化逻辑在共享引擎 static/upload/cos-uploader.js（工作台与
    // /tools/slides 同一份）；唯一权威仍是 bootstrap.capabilities.cos_upload
    //（app.py _cos_upload_capability_payload）。capability off 时只下发
    // {available:false,...}——available 非 true 直接 null，前端不得因文件大
    // 而自行启用 COS（§5）。缺字段/结构非法一律 null：宁可不走 COS，也不拿
    // 坏参数拼请求（D3：十进制字节整数原样使用，前端不自算另一份上限）。
    if (!window.HP_COS_UPLOAD) return null;
    try {
      var caps = window.HP_APP_BOOTSTRAP && window.HP_APP_BOOTSTRAP.capabilities;
      return window.HP_COS_UPLOAD.resolveConfig(caps && caps.cos_upload);
    } catch (e) {
      return null;
    }
  }

  var COS_UPLOAD_CONFIG = resolveCosConfig();

  // U3：手动选路开关退役（统一 COS，无按大小/格式分流）。
  function cosUploadEligible(file) {
    // 词表/上限全部来自服务端 capability（注册表派生 + 产品上限）——前端
    // 不再维护第二份格式例外（zip/conversion 由服务端受理，裸 mrxs 由
    // 服务端拒绝并提示打包）。
    if (!COS_UPLOAD_CONFIG) return false;
    if (!file || typeof file.size !== "number") return false;
    if (file.size <= 0 || file.size > COS_UPLOAD_CONFIG.max_size_bytes) return false;
    var name = file.name || "";
    var ext = name.slice(name.lastIndexOf(".") + 1).toLowerCase();
    return COS_UPLOAD_CONFIG.formats.indexOf(ext) >= 0;
  }

  // When the browser converter is unavailable, the server's existing raster
  // reader can accept ordinary images directly. Require its advertised direct
  // capability as well as the normal size/format gate; all other conversion
  // inputs remain on the existing refusal path. The uploader still verifies
  // the file header and the server performs full decode and pixel-limit checks.
  function canDirectUploadRaster(file) {
    if (!file || !/\.(?:jpe?g|bmp)$/i.test(file.name || "") ||
        !cosUploadEligible(file)) return false;
    var caps = window.HP_APP_BOOTSTRAP && window.HP_APP_BOOTSTRAP.capabilities;
    var direct = caps && caps.cos_upload && caps.cos_upload.direct_upload;
    var ext = file.name.slice(file.name.lastIndexOf(".") + 1).toLowerCase();
    return !!(direct && Array.isArray(direct.formats) &&
              direct.formats.indexOf(ext) >= 0);
  }

  function cosIneligibleReason(file) {
    if (file && typeof file.size === "number" &&
        (file.size <= 0 || file.size > COS_UPLOAD_CONFIG.max_size_bytes)) {
      return tt("upload.cos.err.too_large");
    }
    return tt("upload.cos.err.format_unsupported");
  }

  // ---------- 刷新恢复（§5：只存非秘密 job id 与文件提示） ----------
  // 签名 URL 短 TTL 且属凭证，绝不落 localStorage；隐私模式写入可能抛错。
  function cosJobsRead() {
    try {
      var arr = JSON.parse(localStorage.getItem(COS_JOBS_KEY) || "[]");
      if (!Array.isArray(arr)) return [];
      return arr.filter(function (j) {
        return j && typeof j.job_id === "string" && j.job_id &&
          typeof j.filename === "string" && typeof j.size === "number" &&
          Array.isArray(j.confirmed);
      });
    } catch (e) {
      return [];
    }
  }

  function cosJobsWrite(jobs) {
    try { localStorage.setItem(COS_JOBS_KEY, JSON.stringify(jobs)); }
    catch (e) { /* localStorage 不可用（隐私模式）：仅失去刷新恢复 */ }
  }

  function cosJobSave(job) {
    if (!job || !job.job_id) return;
    var jobs = cosJobsRead().filter(function (j) { return j.job_id !== job.job_id; });
    // P2 合同 §5.3：COS jobs 记录补 slide_id 字段位（匹配/续传仍按 job_id；
    // slide_id 随 status 响应出现时由 drive() 回填）
    // review #1：记录补账号绑定与分片内容摘要（SHA-256）——续传不再凭
    // 名称/大小复用，逐片核验后只跳过已验证分块
    jobs.push({
      job_id: job.job_id, filename: job.filename, size: job.size,
      slide_id: job.slide_id || null,
      account: typeof job.account === "string" ? job.account : "",
      digests: job.digests && typeof job.digests === "object" ? job.digests : {},
      // 摘要方案标记（复核第二轮）：无标记/其他方案 = legacy，续传弃旧
      digest_scheme: typeof job.digest_scheme === "string" ? job.digest_scheme : "",
      confirmed: (job.confirmed || []).slice().sort(function (a, b) { return a - b; }),
    });
    cosJobsWrite(jobs);
  }

  // 只删任务记录（pt.cos.jobs），不碰目标快照——快照消费（fbUpAssocTake）
  // 是身份域的一次性读端，由各完成路径明确择时调用（R5 复核：恢复完成路径
  // 必须等身份就绪后先 Take 再清记录；两条请求的相对速度不得影响结果）。
  function cosJobsDrop(jobId) {
    if (!jobId) return;
    cosJobsWrite(cosJobsRead().filter(function (j) { return j.job_id !== jobId; }));
  }

  // 任务记录 + 快照一并清理：同 Tab 完成/取消/终态（uploadFileCos 行与
  // pollRestoredRow 终态分支）此时身份已知（或 Take 退化为无键空操作），
  // Take 即消费。恢复完成路径（pollRestoredRow viewable）不走这里。
  function cosJobRemove(jobId) {
    if (!jobId) return;
    cosJobsDrop(jobId);
    fbUpAssocTake(jobId);
  }

  function cosFindResumableJob(file) {
    // 同名同大小才视为同一候选；review #1：记录绑定账号——他号记录绝不
    // offered/used（字节内容核验在共享引擎：逐片比对 SHA-256 后才跳过；
    // 本地记录只是候选，真正的身份凭证是分片摘要）
    var jobs = cosJobsRead();
    for (var i = 0; i < jobs.length; i++) {
      if (jobs[i].filename === (file && file.name) &&
          jobs[i].size === (file && file.size)) {
        var recAccount = typeof jobs[i].account === "string" ? jobs[i].account : "";
        if (recAccount !== String(currentUserId || "")) continue;
        return jobs[i];
      }
    }
    return null;
  }

  function cosReadJobRecord(jobId) {
    // review #1：续传记录完整形态（已确认分块 + 分片摘要 + 账号）
    var jobs = cosJobsRead().filter(function (j) { return j.job_id === jobId; });
    if (!jobs.length) return Promise.resolve(null);
    var j = jobs[0];
    return Promise.resolve({
      confirmed: j.confirmed || [],
      digests: j.digests && typeof j.digests === "object" ? j.digests : null,
      digest_scheme: typeof j.digest_scheme === "string" ? j.digest_scheme : "",
      account: typeof j.account === "string" ? j.account : "",
    });
  }

  // ---------- COS PUT 独立传输 ----------
  // C4：putPart/分块计划/状态机已原样移入共享引擎 static/upload/cos-uploader.js
  //（window.HP_COS_UPLOAD；index.html 在 app.js 之前加载）。语义见引擎头注释：
  // COS PUT 绝不经 apiFetch（CSRF 对 COS 是污染头），mode:"cors" +
  // credentials:"omit"；控制 API 经 apiFetch 带双提交头。

  // 检测到的文件内容类型 → 本地化名称（upload-liveness：魔数不符文案的
  // {type} 形参；i18n.js 不收机器枚举，页面侧小映射）。
  var MAGIC_TYPE_NAMES = {
    png: { zh: "PNG 图片", en: "a PNG image" },
    webp: { zh: "WebP 图片", en: "a WebP image" },
    gif: { zh: "GIF 图片", en: "a GIF image" },
    heic: { zh: "HEIC/HEIF 图片", en: "a HEIC/HEIF image" },
    jpeg: { zh: "JPEG 图片", en: "a JPEG image" },
    tiff: { zh: "TIFF 图像", en: "a TIFF image" },
    bmp: { zh: "BMP 图片", en: "a BMP image" },
    zip: { zh: "ZIP 压缩包", en: "a ZIP archive" },
    unknown: { zh: "无法识别的内容", en: "unrecognized content" },
  };

  function magicTypeName(detected) {
    var lang = (window.HP_I18N && window.HP_I18N.getLang()) || "zh";
    var e = MAGIC_TYPE_NAMES[detected] || MAGIC_TYPE_NAMES.unknown;
    return e[lang] || e.zh;
  }

  // ---------- 稳定机器码 → 可读文案（未知码保留原文的兜底模式） ----------
  /// `convert_in_browser`（服务端识别漏判）：面板内转换会自动回队处理；该
  /// 文案不再携带转换器页面链接（2026-10-20 起工作台绝不跳转换器页上传）。
  function convertInBrowserMessage() {
    return tt("upload.cos.err.convert_in_browser");
  }

  /// 服务端跨账号核对拒绝（P1 cross-account 2026-10-15）：POST /api/ingestions
  /// 返回 409 account_mismatch（error / code 任一形态——旧部署只带 error）。
  function isAccountMismatchError(err) {
    return !!(err && err.status === 409 && err.data &&
              (err.data.code === "account_mismatch" ||
               err.data.error === "account_mismatch"));
  }

  /// 存储容量文本（2026-11-04）：字节 → 「10 GB」「1.5 GB」（一位小数）。
  /// 配额与用量都按 1024^3 计（与后端字节口径一致）；非法/缺失返回 null，
  /// 调用方走不带数字的通用文案。
  function storageGbText(bytes) {
    if (bytes === null || bytes === undefined || bytes === "") return null;
    var n = Number(bytes);
    if (!isFinite(n) || n < 0) return null;
    var gib = 1024 * 1024 * 1024;
    var tenths = Math.round(n * 10 / gib);
    return (tenths % 10 === 0 ? String(tenths / 10)
                              : (tenths / 10).toFixed(1)) + " GB";
  }

  // 所有能到达用户的存储配额超限码（主站 COS 通道 + 插件 v1/百度导入的
  // 防御性兜底；服务端在终态响应中附带实际 quota_bytes）。
  var COS_QUOTA_CODES = {
    local_quota_infeasible: 1,
    zip_quota_exceeded: 1,
    upload_quota_exceeded: 1,
    quota_exceeded: 1,
  };

  function cosErrorMessage(status, data) {
    var code = (data && (data.code || data.error)) || "";
    if (COS_QUOTA_CODES[code]) {
      var quota = storageGbText(data && data.quota_bytes);
      return quota ? tt("upload.cos.err.quota", { quota: quota })
                   : tt("upload.cos.err.quota.generic");
    }
    if (code === "upload_too_large") return tt("upload.cos.err.too_large");
    // 跨账号核对拒绝（P1 cross-account）：任务未创建，明确指出登录身份变化
    if (code === "account_mismatch") {
      return tt("upload.cos.err.account_mismatch");
    }
    if (code === "cos_pool_below_product_limit") return tt("upload.cos.err.pool_config");
    if (code === "cos_format_unsupported") return tt("upload.cos.err.format_unsupported");
    // 阶段 1：直传关闭格式（旧码 conversion_moved_to_browser 同文案兼容）
    if (code === "convert_in_browser" ||
        code === "conversion_moved_to_browser") {
      return convertInBrowserMessage();
    }
    if (code === "cos_waiting_limit") return tt("upload.cos.err.waiting_limit");
    if (code === "ingestion_state_conflict") return tt("upload.cos.err.state");
    if (code === "cos_sign_rate_limited") return tt("upload.cos.err.rate");
    if (code === "cos_capacity_reconcile_required") return tt("upload.cos.err.reconcile");
    if (code === "plan_size_mismatch") return tt("upload.cos.err.plan");
    if (code === "name_unavailable") return tt("upload.err.name");
    if (code === "invalid_declared_size") return tt("upload.err.size_mismatch");
    if (code === "cos_unavailable") return tt("upload.cos.err.unavailable");
    // upload-liveness（0084）：vanished client 的终态收口给出明确指引。
    if (code === "client_gone") return tt("upload.cos.err.client_gone");
    if (code === "client_idle_timeout") {
      return tt("upload.cos.err.client_idle_timeout");
    }
    // 校验失败：fail_detail（白名单细节）在时给针对性文案——最常见的是
    // PNG/WebP 改名 .jpg（raster_format_mismatch）。
    if (code === "validation_failed") {
      if (data && data.fail_detail === "raster_format_mismatch") {
        return tt("upload.cos.err.raster_mismatch");
      }
      return tt("upload.cos.err.validation");
    }
    if (status === 403) return tt("upload.err.csrf");
    // 未知码保留原文（排障需要机器码，不猜测语义）
    return code || status || tt("upload.stage.failed");
  }

  // 终态失败码/细节 → 文案键（实时错误分支与刷新恢复分支**共用同一映射**：
  // 复核修复前恢复路径一律显示通用「上传失败」，client_gone /
  // client_idle_timeout / raster_format_mismatch 的针对性文案只在实时分支
  // 存在——刷新恢复恰是「用户离开后重新进来」的事故场景，正是最需要它）。
  // 未知码返回 null（调用方保留原码或通用文案，不猜测语义）。
  function cosFailStageKey(code, detail) {
    if (COS_QUOTA_CODES[code]) return "upload.cos.stage.quota_full";
    if (code === "client_gone") return "upload.cos.err.client_gone";
    if (code === "client_idle_timeout") return "upload.cos.err.client_idle_timeout";
    if (code === "validation_failed") {
      if (detail === "raster_format_mismatch") return "upload.cos.err.raster_mismatch";
      return "upload.cos.err.validation";
    }
    return null;
  }

  // 阶段名 → 文案键（§5 固定六段 + terminal/未知兜底）
  function cosStageKey(stage) {
    var known = { waiting_space: 1, uploading: 1, awaiting_server: 1,
                  downloading: 1, validating: 1, processing: 1,
                  readiness: 1, viewable: 1 };
    return known[stage] ? "upload.cos.stage." + stage : "upload.stage.failed";
  }

  /// 服务端阶段 → 行视图（**直传与转换产物共用**的映射，P2 复核 2026-10-20
  /// 「其他差异 1」：转换产物的 status 回调此前只传阶段名，丢掉下载分母与
  /// 不定态指示——现在两处都走这一个纯函数）。用户口径固定五步：
  /// 服务器接收/校验/处理/准备可查看统一显示「服务器正在处理」，仅在
  /// 当前阶段确有真实分母（服务器接收 checkpoint 的 downloaded_bytes /
  /// declared_size）时显示百分比；其余阶段一律不定态活动指示，绝不把接收
  /// 字节百分比冒充完整处理进度，也绝不虚构定时器进度。
  function cosStageView(b, noteOverride) {
    var frac = null;
    var indeterminate = false;
    if (b && b.stage === "downloading" && typeof b.downloaded_bytes === "number" &&
        typeof b.declared_size === "number" && b.declared_size > 0) {
      frac = Math.min(b.downloaded_bytes / b.declared_size, 1);
    } else if (b) {
      // 上传（恢复行/探测期）与其余服务端阶段：无字节量化 → 不定态活动指示；
      // 上传中的字节事件由引擎的 progress 分支另行驱动
      indeterminate = true;
    }
    var note = noteOverride || "";
    if (!note && b && b.stage === "waiting_space" &&
        typeof b.queue_position === "number") {
      // 排队位置 0 基 → 人类序数；绝不显示预计时间（§6.1 不承诺 ETA）
      note = tt("upload.cos.queue", { n: b.queue_position + 1 });
    }
    if (!note && b && b.stage === "processing") {
      // U2 形态细节：zip 逐 item 计数 / conversion 子任务状态（纯展示）
      if (b.items && b.items.length) {
        var pub = 0;
        for (var i = 0; i < b.items.length; i++) {
          if (b.items[i].state === "published") pub++;
        }
        note = tt("upload.cos.items", { n: pub, m: b.items.length });
      } else if (b.conversion && b.conversion.state) {
        note = tt("upload.cos.conv_state", { s: b.conversion.state });
      }
    }
    return { key: cosStageKey(b && b.stage), frac: frac,
             indeterminate: indeterminate, note: note };
  }

  function cosShowStage(row, b, noteOverride) {
    var view = cosStageView(b, noteOverride);
    row.setStage(view.key, view.frac, view.note, view.indeterminate);
  }

  // COS 行操作按钮（取消/重试/改用平台上传）：现有上传行无按钮先例，
  // 借 upload-item 的紧凑文本习惯用行内样式兜底（不改样式表）
  function addRowButton(row, label, onClick) {
    if (!row || !row._row || !row._row.appendChild) return null;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "upload-item-btn";
    btn.textContent = label;
    btn.style.marginTop = "4px";
    btn.style.fontSize = "11px";
    btn.style.padding = "2px 8px";
    btn.style.cursor = "pointer";
    btn.addEventListener("click", function (e) {
      if (e && e.preventDefault) e.preventDefault();
      onClick(e);
    });
    row._row.appendChild(btn);
    return btn;
  }

  // 服务端冻结计划 {part_number,length} → 前端切片表：cosBuildPlan 已随状态机
  // 移入共享引擎（HP_COS_UPLOAD.buildPlan，供测试直用）。

  function uploadFileCos(file, row, opts) {
    // C4：本函数只是工作台侧行 UI 适配——状态机/请求序列在共享引擎
    // static/upload/cos-uploader.js（window.HP_COS_UPLOAD.createUpload）。
    // 持久化经注入适配器落到 localStorage pt.cos.jobs（§5：只存非秘密 job id
    // 与文件提示；签名 URL 短 TTL 且属凭证，绝不落 localStorage）。
    opts = opts || {};
    // P2 复核 2026-10-20（问题 1）：**唯一结果通道**——本函数所有「引擎尚未
    // 创建」的出口（引擎缺失、等锁被取消、拿到锁后发现已取消）都必须恰好
    // 收口一次：行 UI 就地收尾，结果经 opts.onOutcome 交回编排器，让它释放
    // 上传槽、调度下一条、且绝不新建服务器任务。没有这条通道时（旧代码）
    // 等锁取消只 abort 等待就返回，外层 Promise 永不收口——上传槽被永久
    // 占住，后续文件全部停在排队（复核脚本 cancelDuringContentLock）。
    // 同一通道也保证**迟到回调**（取消后才 resolve 的锁/引擎回执）不能再
    // 二次收口：outcomeSettled 恒为闸。
    var outcomeSettled = false;
    var inflightJobId = null;   // 本行当前服务器任务（收口时从在传表清除）
    function entryCancelled() {
      if (opts.isCancelled && opts.isCancelled()) return true;
      return !!(lockAbort && lockAbort.signal && lockAbort.signal.aborted);
    }
    function settleOutcome(body, err) {
      if (outcomeSettled) return;
      outcomeSettled = true;
      if (inflightJobId) delete cosInflightJobs[inflightJobId];
      dropRowCancelBtn();   // 终态：行上的「取消」一并摘除
      dropVisPoke();        // 注销可见性触点（别留悬空监听）
      if (opts.onOutcome) opts.onOutcome(body, err);
    }
    if (!window.HP_COS_UPLOAD || !COS_UPLOAD_CONFIG) {
      row.markError();
      row.setStage("upload.stage.failed");
      row.finish(10000);
      // P2 问题 1 的同族出口：引擎缺失也是「建引擎前失败」——面板条目同样
      // 必须收到结果（否则条目永久停在 uploading 占住上传槽）。
      settleOutcome(null, { data: { code: "cos_unavailable" } });
      return;
    }
    // 跨账号归属绑定（P1 cross-account 2026-10-15）：本次上传开始时**冻结**
    // 的页面身份（本页缓存、未经确认不会变）。它就是该条目的归属证据：
    //   - 记录持久化 / 跨标签内容锁的账号段用它（下面引擎 opts.account）；
    //   - 新任务创建体经引擎带 expected_account（服务端按当前会话核对，
    //     不符 409 account_mismatch——前端缓存不可靠，服务器是唯一权威）。
    // 绝不在请求发出前现读 currentUserId：身份中途被刷新而条目未重判时，
    // 现读值会把产物静默记到新账号名下。
    var frozenAccount = String(currentUserId || "");
    var jobId = opts.resumeJobId || null;   // created 事件回填（重试按钮用）
    var upload = null;
    // 复核修复：页面可见性恢复时补一拍在场证据（心跳的驱动源只有 JS 事件
    // 循环，标签被挂起期间停摆——重新可见即刷新 client_seen_at，缩小服务端
    // 活性 sweep 误杀「XHR 仍在传但 JS 被冻结」中间态的窗口）。监听随本行
    // 的 upload 收口注销；引擎缺失/取消后 pokeLifecycle 是空操作。
    var onVisPoke = function () {
      if (typeof document !== "undefined" && document.visibilityState &&
          document.visibilityState !== "visible") return;
      if (upload && typeof upload.pokeLifecycle === "function") {
        try { upload.pokeLifecycle(); } catch (e) {}
      }
    };
    if (typeof document !== "undefined" && document.addEventListener) {
      document.addEventListener("visibilitychange", onVisPoke);
    }
    function dropVisPoke() {
      if (typeof document !== "undefined" && document.removeEventListener) {
        document.removeEventListener("visibilitychange", onVisPoke);
      }
    }
    // U1 字节级进度：上传阶段百分比按已发送/已确认字节（不是分片数）；
    // 重试期间显示「正在重试」，body 全部发出且未确认时显示
    // 「数据已发送，等待确认」——绝不显示「完成」。
    var retrying = false;
    function cosProgressNote(ev) {
      if (ev && ev.sentAll) {
        return tt("upload.cos.sent_all") +
          (retrying ? " · " + tt("upload.cos.retrying") : "");
      }
      if (ev && typeof ev.loadedBytes === "number" &&
          typeof ev.totalBytes === "number" && ev.totalBytes > 0) {
        var note = tt("upload.cos.bytes", {
          done: humanSize(ev.loadedBytes), total: humanSize(ev.totalBytes),
        });
        if (retrying) note += " · " + tt("upload.cos.retrying");
        return note;
      }
      return retrying ? tt("upload.cos.retrying") : "";
    }

    // —— 取消（§4 cancel 幂等）：停轮询 + abort 在途 PUT + POST cancel ——
    // 复核 8428f7f0：等锁中取消 = abort 内容锁等待（此刻引擎尚未创建）
    var lockAbort = (typeof AbortController === "function")
      ? new AbortController() : null;
    // 钮句柄留着：终态收口时摘除行上的「取消」（P3 复核 2026-10-18）
    var cancelBtn =
    addRowButton(row, tt("upload.cos.cancel"), function () {
      // 终态守卫（P3 复核 2026-10-18）：上传已收口（入库完成/失败/已取消）
      // 后，行上残留的「取消」必须是空操作——绝不把完成行翻回「已取消」，
      // 也绝不补发 cancel 请求（服务端对已发布资产只会拒绝）。摘除由下面的
      // done 收口挂钩负责（三种终态同一路径）。
      if (outcomeSettled) return;
      if (upload && typeof upload.settled === "function" && upload.settled()) {
        return;
      }
      if (lockAbort) { try { lockAbort.abort(); } catch (e) {} }
      if (!upload) return;   // 等锁中：abort 由锁等待的 reject 分支收口（下同）
      row.setStage("upload.cos.cancelled");
      row.finish(10000);
      upload.cancel();
    });
    // 摘除行上的「取消」钮（终态收口用。宿主无按钮/已摘除时空操作）。
    function dropRowCancelBtn() {
      if (cancelBtn && cancelBtn.parentNode && cancelBtn.parentNode.removeChild) {
        cancelBtn.parentNode.removeChild(cancelBtn);
      }
    }

    // 复核 8428f7f0（双标签并发直传同一文件）：行/引擎没有跨标签互斥，两
    // 个工作台标签并发拖入同一文件会各建一个 ingestion。与工具页同一把跨
    // 标签内容锁（账号 + 文件身份，共享引擎 contentLockName）；锁内引擎自
    // 查续传候选（另一标签进行中 → 询问后续传，绝不并发双建）。等待期行
    // 显示「另一标签正在上传」，可取消；Web Lock 不可用（旧浏览器/非安全
    // 上下文）→ 既有行为。已完成文件不在此去重（工作台重新上传同一文件 =
    // 新建切片，工作台没有已发布回执语义）。
    var locked = (window.HP_COS_UPLOAD.acquireContentLock &&
                  window.HP_COS_UPLOAD.contentLockName)
      ? window.HP_COS_UPLOAD.acquireContentLock(
          window.HP_COS_UPLOAD.contentLockName(frozenAccount, file),
          { signal: lockAbort ? lockAbort.signal : undefined,
            onWaiting: function () {
              row.setStage("upload.cos.other.tab", null, "", true);
            } })
      : Promise.resolve({ held: false, release: function () {} });
    locked.then(function (lk) {
      // 取消边界（独立复核 #3 问题 1 的顺查）：内容锁等待期间被面板取消
      // （编排器 cancel → entry.cancelled，此刻引擎尚未创建、无处停传）→
      // 不建引擎、不 POST /api/ingestions，释放刚拿到的锁并以取消回执收口。
      // P2 问题 1 修复：取消判定同时看行「取消」的 abort 信号（等锁取消在
      // 旧代码只走下面的 reject 分支且不收口——那是卡槽 bug 本体）。
      if (entryCancelled()) {
        if (lk && lk.release) { try { lk.release(); } catch (e) {} }
        settleOutcome(null, { cancelled: true, __cancelled: true });
        return;
      }
      upload = window.HP_COS_UPLOAD.createUpload({
        source: file,
        apiFetch: apiFetch,
        config: COS_UPLOAD_CONFIG,
        // 阶段 1：创建 ingestion 时携带嗅探出的直传类别声明（服务端词表校验，
        // worker 在 open_slide 前头级核验；无声明则不带字段——存量语义不变）
        createBody: opts.directClass ? { direct_class: opts.directClass } : null,
        storage: {
          save: cosJobSave,
          complete: cosJobRemove,   // viewable/终态：本地恢复记录清理（§5）
          remove: cosJobRemove,
          findResumable: cosFindResumableJob,
          readConfirmed: function (id) {
            var jobs = cosJobsRead().filter(function (j) { return j.job_id === id; });
            return jobs.length ? jobs[0].confirmed : [];
          },
          readRecord: cosReadJobRecord,   // review #1：分片摘要 + 账号
        },
        // review #1：记录/候选按登录账号绑定（上传开始时冻结的账号——
        // 也是创建体 expected_account 的来源；免认证态为空串——绑定语义
        // 退化为「同一（唯一）账号」，且不带 expected_account 字段）
        account: frozenAccount,
        resumeJobId: jobId,
        skipConfirm: !!opts.skipConfirm,
        confirmResume: function () {
          return window.confirm(tt("upload.cos.resume_confirm", { name: file.name }));
        },
        // 工作台保持抽出前行为：完成请求网络失败不自动重发（行级失败提示
        // 重选同名文件续传）；工具页才开 retryCompleteOnNetworkError。
        retryCompleteOnNetworkError: false,
        useResumeEndpoint: false,
        // 复核修复：心跳（活性探测）走不跳转 fetch——与分块签名/状态推进
        // 不同，401 不得在上传中途把页面导航去 /login。
        heartbeatFetch: apiFetchNoRedirect,
        onEvent: function (ev) {
          if (ev.type === "status") {
            cosShowStage(row, ev.body);
            if (ev.body && ev.body.stage !== "uploading") retrying = false;
          } else if (ev.type === "progress") {
            // phase:'uploading' 字节事件（兼容旧 frac 消费；determinate=false →
            // 不定态 + 已确认字节后备，不虚构按片百分比）
            retrying = false;
            row.setStage("upload.cos.stage.uploading",
                         ev.determinate === false ? null : ev.frac,
                         cosProgressNote(ev), ev.determinate === false);
          } else if (ev.type === "retry") {
            retrying = true;
            row.setStage("upload.cos.stage.uploading", null,
                         cosProgressNote(null), true);
          } else if (ev.type === "created") {
            jobId = ev.jobId;
            // 后台任务摘要（抽屉）的在传登记：收口时清除（settleOutcome）
            inflightJobId = ev.jobId;
            cosInflightJobs[ev.jobId] = file.name || "?";
            // P2 问题 2：把服务器任务 id 交回条目（面板直传条目据此提供
            // 「同任务续传」——没有它，失败后只剩「申请支持该格式」）。
            if (typeof opts.onJobCreated === "function") {
              try { opts.onJobCreated(ev.jobId); } catch (e) { /* 不影响上传 */ }
            }
            // folder-experience §1.3×§5：ingestion 建成即把上传开始时冻结的
            // 目标快照按任务 ID 落盘——刷新后恢复行完成仍按原目标关联
            fbUpAssocSave(jobId, opts.assocTarget);
          }
        },
      });

      // done 收口（成功/失败/取消）后立刻释放内容锁：等待标签随后重读
      // 回执/记录（工具页）或续传候选（工作台）。
      if (opts.onEngine) {
        // 面板直传条目：把引擎句柄交回调用方（取消按钮才能真正停传 +
        // POST /cancel；否则「取消」只改行文案，后台传输照跑并最终翻回完成）。
        try { opts.onEngine(upload); } catch (e) { /* 回调失败不影响上传 */ }
      }
      // 终态摘除「取消」钮（P3 复核 2026-10-18）：直传行的取消钮在面板
      // 编排器按钮宿主之外，done 收口（入库完成/失败/取消）后残留可点会
      // 把完成行翻回「已取消」并补发 cancel 请求——随收口一并摘除。
      if (upload.done && typeof upload.done.then === "function") {
        upload.done.then(dropRowCancelBtn, dropRowCancelBtn);
      }
      var settled = upload.done.then(function (r) {
        if (!r || r.cancelled) {
          settleOutcome(null, { cancelled: true, __cancelled: true });
          return;
        }
        if (opts.onOutcome) {
          // 面板直传条目：结果交回编排器收口（行 UI/关联/打开由面板负责），
          // 不在这里刷新列表与打开切片（避免与面板回调重复）
          settleOutcome(r.body, null);
          return;
        }
        cosUploadSucceeded(row, r.body, file, opts.assocTarget);
      }, function (err) {
        if (opts.onOutcome) {
          if (isAccountMismatchError(err)) {
            // 面板编排器按服务端核对结果收口 account_mismatch（改绑/删除）：
            // 把本次创建冻结并随 expected_account 发出的账号带上——它就是
            // 「归属账号」的证据（服务器拒绝 = 会话已不是它）。
            err.expectedAccount = frozenAccount;
          }
          settleOutcome(null, err);
          return;
        }
        var msg;
        if (err && err.magicMismatch) {
          // 前置魔数校验拒绝（零网络请求）：说出检测到的内容类型
          msg = tt("upload.cos.err.magic",
                   { type: magicTypeName(err.magicMismatch.detected) });
        } else if (err && err.terminal) {
          msg = cosErrorMessage(0, {
            code: (err.data && err.data.fail_code) || "",
            fail_detail: err.data && err.data.fail_detail,
            // 2026-11-04：配额类终态带实际上限（服务端已附 quota_bytes）
            quota_bytes: err.data && err.data.quota_bytes,
          });
        } else if (err && err.network) {
          // 网络层失败：任务与已确认分块保留，重选同名文件可续传（§5）
          msg = tt("upload.cos.resume_hint");
        } else {
          msg = cosErrorMessage(err && err.status, err && err.data);
        }
        row.markError();
        row.setStage("upload.stage.failed");
        if (err && typeof err.part === "number") {
          // 分块最终失败：从 confirmed 续传（服务端计划仍在，跳过已确认块）
          addRowButton(row, tt("upload.cos.retry"), function () {
            uploadFile(file, { cosRetry: jobId });
          });
        } else if (err && err.recheck) {
          // review #9：状态轮询连续失败放弃 → 只提供「重新检查状态」
          //（同 job 重入状态机重新查询；绝不重建任务/重传分片）
          addRowButton(row, tt("upload.cos.recheck"), function () {
            uploadFile(file, { cosRetry: jobId });
          });
        }
        row.finish(10000);
        toast(t("upload.fail", { e: msg }), "error");
      });
      settled.then(function () { lk.release(); },
                   function () { lk.release(); });
      settled.then(dropVisPoke, dropVisPoke);   // 行收口即注销可见性触点
    }, function () {
      // P2 问题 1（复核 2026-10-20）：等锁被取消（AbortError）必须经**唯一
      // 结果通道**收口一次——引擎尚未创建、无处停传；行就地切「已取消」，
      // 结果交回编排器（槽位释放、下一条调度、不建服务器任务）。旧代码在这
      // 条出口只清可见性监听就返回，外层 Promise 永不收口——上传槽被永久
      // 占住，后续文件全部停在排队（复核脚本 cancelDuringContentLock 复现）。
      row.setStage("upload.cos.cancelled");
      row.finish(10000);
      settleOutcome(null, { cancelled: true, __cancelled: true });
    });
  }

  function cosUploadSucceeded(row, b, file, assocTarget) {
    // viewable：照 V2 commit 后的跳转习惯（完成 → 刷新列表 → 关联 → 打开）。
    // P2 合同 §5.2：打开目标 = 响应 slide_id（唯一）；缺字段才按名回落。
    // U2 多结果：zip 打开首个已发布 item（其余经列表刷新可见）；
    // conversion 产物 slide_id 在 conversion 子视图（ready 才出现）。
    var sid = b.slide_id || null;
    if (!sid && b.items && b.items.length) {
      for (var i = 0; i < b.items.length; i++) {
        if (b.items[i].state === "published") { sid = b.items[i].slide_id; break; }
      }
    }
    if (!sid && b.conversion && b.conversion.slide_id) {
      sid = b.conversion.slide_id;
    }
    var target = uploadedTarget(b, file);
    if (sid) target.id = sid;
    row.setStage("upload.stage.done");
    row.finish();
    toast(t("upload.done", { name: target.name }), "success");
    loadAll();
    // 关联用上传开始时冻结的目标快照（§1.3）：侧栏后续变化不影响归属
    importAssociateUploaded(target.id, target.name, assocTarget || null, row);
    openSlide(target.id || target.name);
  }

  // ---------- 启动：未完任务只读恢复（U3：手动开关退役） ----------
  function initCosUploadUi() {
    // U3（统一 COS）：选路开关/大小分流已删除——新任务恒走 uploadFileCos；
    // 本入口保留刷新恢复（旧任务只读轮询）与未来装配点。
    restoreCosJobs();
  }

  function restoreCosJobs() {
    // 刷新恢复（§5）：未终态任务显示只读进度行继续轮询（浏览器没有文件
    // 句柄，不能替用户续传分块）；uploading 未完成 → 提示重选同名文件续传。
    // 查询失败保留记录，下次启动再试；终态/已可查看/查无此任务即清理。
    cosJobsRead().forEach(function (j) {
      pollRestoredRow(j);
    });
  }

  function pollRestoredRow(job) {
    var done = false;
    var row = null;
    function ensureRow() {
      if (!row) row = makeUploadRow({ name: job.filename, size: job.size });
      return row;
    }
    function tick() {
      apiFetch("/api/ingestions/" + encodeURIComponent(job.job_id))
        .then(jsonBody)
        .then(function (res) {
          if (done) return;
          var b = res.ok ? res.body : null;
          var st = b && b.stage;
          if (!st) { setTimeout(tick, 5000); return; }
          if (st === "viewable") {
            done = true;
            cosJobsDrop(job.job_id);   // 任务记录移除；快照消费延迟到身份就绪（见下，R5）
            var r0 = ensureRow();
            r0.setStage("upload.stage.done");
            r0.finish();
            // P4（slide ID 化）：b.slide 是展示快照（job.slide_canonical_name），
            // 仅用于 toast 文案回落；本响应的 b.slide_id 来自任务绑定
            //（job.slide_id 创建即分配，_ingestion_state_body 直读）——打开
            // 目标一律按 slide_id，不按名猜。
            toast(t("upload.done", { name: b.slide || job.filename }), "success");
            loadAll();
            // folder-experience §1.3×§5：恢复轮询常先于 /api/auth/info 完成，
            // 而目标快照按身份域存取——身份就绪后**先取快照**（一次性消费），
            // 再按已取得的原快照关联（R5 复核：auth/info 与 ingestion 谁先
            // 返回都恰好消费一次；此处绝不再调 cosJobRemove——那会提前 Take
            // 掉快照，身份先就绪时第二次 Take 落空 → 静默丢失关联）。
            //（失败进入既有待办/行内重试机制）；快照缺失/过期/他号 → 不猜，
            // 保持未归类，绝不改成当前目录，也绝不重传已发布切片。
            fbIdentityReady().then(function () {
              var restoredTarget = fbUpAssocTake(job.job_id);
              importAssociateUploaded(
                b.slide_id || null, b.slide || job.filename,
                restoredTarget || { pid: "", newProjectName: "" },   // 无快照 = 显式未归类，不回退活会话状态
                r0);
            });
            return;
          }
          if (st === "terminal") {
            done = true;
            cosJobsDrop(job.job_id);   // 任务记录立即清理（§5：终态即清理）
            var r1 = ensureRow();
            r1.markError();
            // 复核修复：终态恢复行必须消费 b.fail_code / b.fail_detail——与
            // 实时错误分支同一映射（cosFailStageKey）。通用「上传失败」会
            // 把 client_gone / client_idle_timeout / raster_format_mismatch
            // 的针对性指引丢在刷新恢复路径上（用户离开后回来恰是本次事故
            // 场景）。toast 与实时路径同形（upload.fail {e}）：行 10s 后自
            // 行移除，原因不能只留在行里。
            var failMsg = cosErrorMessage(0, {
              code: b.fail_code || "", fail_detail: b.fail_detail,
              // 2026-11-04：刷新恢复的配额终态同样显示实际上限
              quota_bytes: b.quota_bytes,
            });
            r1.setStage(cosFailStageKey(b.fail_code, b.fail_detail) ||
                        "upload.stage.failed");
            r1.finish(10000);
            toast(t("upload.fail", { e: failMsg }), "error");
            // 快照清理同恢复完成路径：等身份就绪后按本号域消费（与轮询、
            // auth/info 谁先返回无关）；取消/终态绝不保留遗留快照。
            fbIdentityReady().then(function () {
              fbUpAssocTake(job.job_id);
            });
            return;
          }
          var note = (st === "uploading") ? tt("upload.cos.resume_hint") : "";
          cosShowStage(ensureRow(), b, note);
          setTimeout(tick, 3000);
        })
        .catch(function () {
          if (!done) setTimeout(tick, 5000);
        });
    }
    tick();
  }

  // =========================================================================
  // 上传面板内转换（docs/upload-inline-convert-design-20261015.md）
  //
  // 编排器本体在 static/upload/inline-convert.js（window.HP_INLINE_CONVERT，
  // classic script、无 DOM/无网络、可 vitest 直测）；这里只做工作台侧的粘合：
  // 行渲染与按钮（§6.3 是按钮全集的唯一权威）、运行器动态 import、目标快照、
  // 完成时的关联与打开、申请支持、beforeunload。
  //
  // 开关关闭（inline_convert.enabled=false）或引擎初始化失败 → 需要转换的
  // 文件行内说明 + 重试/支持（绝不跳转换器页；2026-10-20 回退语义改定）。
  // =========================================================================

  //: 运行器动态 import 的 URL（唯一常量；生产/测试共用）
  var RUNNER_URL = "/static/tools/slide-transform/runner.js";
  var inlineRunner = null;        // SlideToolsRunner 实例（懒创建）
  var inlineEngine = null;        // engine.js 命名空间（懒 import）
  var inlineOrch = null;          // 面板编排器
  var inlineInitP = null;         // 初始化 promise（并发去重）

  /// inline_convert 能力（app.py _inline_convert_payload 唯一权威）。
  function inlineConvertCaps() {
    try {
      var caps = window.HP_APP_BOOTSTRAP && window.HP_APP_BOOTSTRAP.capabilities;
      var cfg = window.HP_INLINE_CONVERT
        ? window.HP_INLINE_CONVERT.normalizeConfig(caps) : null;
      return cfg;
    } catch (e) { return null; }
  }

  function inlineEnabled() {
    var cfg = inlineConvertCaps();
    return !!(cfg && cfg.enabled);
  }

  /// 懒初始化：动态 import 运行器 + 引擎（classic script 里可以 import；
  /// /app 的 CSP 实测放行 wasm/worker，见设计 §10.2）。失败 → 行内说明 +
  /// 回退到弹窗交接（§11 R3）。
  function ensureInlineOrch() {
    if (inlineOrch) return Promise.resolve(inlineOrch);
    if (inlineInitP) return inlineInitP;
    if (!window.HP_INLINE_CONVERT) return Promise.resolve(null);
    inlineInitP = Promise.all([
      import(RUNNER_URL),
      import("/static/tools/slide-transform/engine.js"),
    ]).then(function (mods) {
      var runnerMod = mods[0];
      inlineEngine = mods[1];
      return runnerMod.SlideToolsRunner.create();
    }).then(function (runner) {
      inlineRunner = runner;
      inlineOrch = window.HP_INLINE_CONVERT.createOrchestrator({
        runner: runner,
        engine: inlineEngine,
        sniff: window.HP_SLIDE_SNIFF,
        cos: window.HP_COS_UPLOAD,
        caps: inlineCaps(),
        ui: {
          row: inlineRowRender,
          setStage: inlineRowSetStage,
          markError: inlineRowMarkError,
          buttons: inlineRowButtons,
          toast: function (key, vars, kind) {
            // 形参值可能是 {__i18n: key} 包装（面板把文案键作为值传递的
            // 唯一合法形式）：先解析内层键再翻译外层，避免把键原文显示给用户。
            toast(tt(key, inlineResolveVars(vars)), kind || "info");
          },
          refresh: function () {
            applyBeforeunload();
            // P2 问题 3：本机队列变化立即重画抽屉「后台任务」的本机部分
            //（服务器部分沿用缓存，不添网络请求）
            refreshImportTasksLocalOnly();
          },
          remove: inlineRowRemove,
        },
        account: function () { return String(currentUserId || ""); },
        // 独立复核 #3/#5：身份就绪信号——恢复行的归属分类等它（未知身份
        // ≠ 他人；获取失败不落定）；落定/账号变化时 markAuthInfoSettled
        // 也会调 recheckOwnership 重判既有行。
        identityReady: authInfoReady,
        // 复核 #5：服务端是否明确启用认证（空归属记录 = 归属未知哨兵只在
        // 启用认证的部署生效）
        authEnabled: function () { return authInfoEnabled; },
        // 复核 #5：identity_wait 行的手动「重试」→ 立即重拉 auth/info
        retryIdentity: retryAuthInfoNow,
        t: function (key, vars) { return tt(key, inlineResolveVars(vars)); },
        typeName: function (detected) { return magicTypeName(detected); },
        target: function () { return importTargetSnapshot(); },
        onArtifact: uploadEntryArtifact,
        cancelArtifact: function (entry) {
          if (entry && entry._upload && entry._upload.cancel) {
            try { entry._upload.cancel(); } catch (e) { /* */ }
          }
        },
        onUploaded: inlineUploaded,
        apiFetch: apiFetch,
        apiFetchNoRedirect: apiFetchNoRedirect,
        setTimer: function (fn, ms) { setTimeout(fn, ms); },
      });
      // 刷新恢复：只列出、不自动开始（§6.2）。等身份信号落定再恢复——
      // 恢复行的归属判定需要真实账号（独立复核 #3 问题 2：与 /api/auth/info
      // 并行启动时，身份未就绪绝不把自己的记录判成 account_mismatch）。
      authInfoReady().then(function () {
        inlineOrch.restore().then(function (made) {
          // 转换器页「保存到我的项目」的移交标记（?save-job=<jobId>）：
          // 只对这一个显式移交的 jobId 解除停放并自动续传一次（用户在
          // 转换器页的点击即同意），随后摘掉 URL 标记；其余恢复行照旧
          // 停放（设计 §6.2 不自动开始的口径不变）。
          resumeHandoffJob(made);
          // 待保存标记（登录/注册绕行，复核 r2 #1）：非阻塞提示继续保存
          checkPendingSave();
          applyBeforeunload();
        });
      });
      return inlineOrch;
    }).catch(function (e) {
      console.error("[inline-convert] 初始化失败（回退到弹窗交接）:", e);
      inlineInitP = null;
      return null;
    });
    return inlineInitP;
  }

  function inlineCaps() {
    var caps = (window.HP_APP_BOOTSTRAP && window.HP_APP_BOOTSTRAP.capabilities) || {};
    var cos = caps.cos_upload || {};
    return {
      inlineConvert: window.HP_INLINE_CONVERT
        ? window.HP_INLINE_CONVERT.normalizeConfig(caps) : null,
      directUploadFormats: (cos.direct_upload && cos.direct_upload.formats) || null,
      browserConvertFormats: (cos.browser_convert && cos.browser_convert.formats) || null,
      cosConfig: resolveCosConfig(),
    };
  }

  /// toast/文案形参里的 i18n 键包装（{__i18n: key}）→ 已翻译文本。
  /// 非包装值原样保留（文件名、机器码、数字）；未提供 i18n 词条时 tt 回落
  /// key 本身，与既有语义一致。
  function inlineResolveVars(vars) {
    if (!vars || typeof vars !== "object") return vars;
    var out = {};
    Object.keys(vars).forEach(function (k) {
      var v = vars[k];
      out[k] = (v && typeof v === "object" && typeof v.__i18n === "string")
        ? tt(v.__i18n) : v;
    });
    return out;
  }

  // ---------- 行渲染（复用 makeUploadRow 的四段 + 按钮组） ----------

  var inlineRowEls = {};   // entry.id -> {row, note, btnHost, kinds}

  function inlineRowRender(entry) {
    if (!entry || inlineRowEls[entry.id]) return;
    var row = makeUploadRow({ name: entry.name, size: entry.size });
    var host = row._row;
    var note = null;
    var btnHost = null;
    if (host && host.appendChild) {
      note = document.createElement("div");
      note.className = "upload-item-status";
      // MRXS 协助/确认/拒绝的行内说明都在这条附注上原地更新——aria-live
      // 让读屏器听到状态变化（设计 §3 的「不只是颜色」同类要求）
      note.setAttribute("aria-live", "polite");
      // 「请保持页面打开」只对**正在进行**的条目显示（设计 §3）；排队/终态
      // 不需要它。文案随状态在 inlineRowButtons / setStage 里刷新。
      note.textContent = inlineRowNoteText(entry);
      host.appendChild(note);
      btnHost = document.createElement("div");
      btnHost.className = "upload-item-actions";
      host.appendChild(btnHost);
    }
    inlineRowEls[entry.id] = { row: row, note: note, btnHost: btnHost };
  }

  /// 行附注：进行中 → 「请保持页面打开」；磁盘待确认 → need/available 数字；
  /// 已提交格式申请 → 编号回执；其余不显示（设计 §3 / §7.1 / §7.3）。
  function inlineRowNoteText(entry) {
    // MRXS 协助（2026-10）：等待补选同名数据文件夹——非错误的中性说明；
    // 确认/拒绝都说明白文件夹名与切片名（读屏经 aria-live 听到变化）
    if (entry.state === "companion_confirm" && entry.companion) {
      return tt("upload.inline.companion.mismatch", {
        folder: entry.companion.folder || "",
        stem: entry.companion.stem || "",
      });
    }
    if (entry.state === "need_folder") {
      if (!folderPickSupported()) {
        // 目录选择器与 webkitdirectory 都没有：不留死按钮，如实说明
        return tt("upload.inline.companion.unsupported");
      }
      var err = entry.companionError || null;
      if (err && err.read) {
        return tt("upload.inline.needFolder.readFailed");
      }
      if (err && err.code === "too_many_members") {
        return tt("upload.inline.needFolder.tooMany", {
          n: String(err.cap || 8192),
        });
      }
      if (err && err.folder) {
        return tt("upload.inline.needFolder.notDataFolder", {
          folder: err.folder,
          stem: (entry.companion && entry.companion.stem) || "",
        });
      }
      // 主提示 + 一句权限说明（浏览器会请求允许读取该文件夹）
      return tt("upload.inline.needFolder.mrxs", {
        stem: (entry.companion && entry.companion.stem) || "",
      }) + " " + tt("upload.inline.companion.hint");
    }
    if (entry.state === "need_disk_confirm" && entry.diskInfo) {
      return tt("upload.inline.needDisk", {
        available: humanSize(entry.diskInfo.available || 0),
        need: humanSize((entry.diskInfo.need && entry.diskInfo.need.total) || 0),
      });
    }
    // 归属不符（独立复核 #2）：行注明归属账号与两条出路（显式改绑 /
    // 删除本地产物），绝不静默续传或新建。
    if (entry.state === "account_mismatch") {
      return tt("upload.inline.account.owner",
        { owner: entry.accountOwner || tt("tools.account.unknown") });
    }
    if (entry.state === "converting" || entry.state === "uploading" ||
        entry.state === "upload_queued" || entry.state === "convert_queued") {
      return tt("upload.inline.note.keepopen");
    }
    // 行内附注可叠加：检测类型（拒绝原因，toast 3 秒即逝 → 行内是唯一持久
    // 证据）与申请支持回执（编号 + 业务状态）**都要**留在行上——二者是同一
    // 行的两条独立信息，谁也不能盖掉谁（设计 §7.1 + §9.4 用例 B）。
    var lines = [];
    if (entry.state === "rejected" && entry.detectedTypeName) {
      lines.push(tt("upload.inline.rejected.magic", { type: entry.detectedTypeName }));
    }
    // 申请支持的**行内回执**（§7.1）：抽屉默认关闭，行内按钮是主入口——
    // 只写抽屉的话用户点完看不到任何结果。
    if (entry.supportRequestId) {
      lines.push(tt("fr.ok.registered", { id: entry.supportRequestId }) + " · " +
        frBusinessStatusLabel(entry.supportBusinessStatus));
    }
    // MRXS 协助装配后的包不完整（planner 缺成员）：convert_failed 行上同时
    // 给「重新选择文件夹」的入口与说明——说明里点名缺哪些文件（复核 r3 B；
    // 清单过长时截断为前 5 个 + 「等 N 个文件」）
    if (entry.state === "convert_failed" && entry.companionRetry &&
        entry.companion) {
      var missingText = missingListText(entry.companionMissing);
      lines.push(missingText
        ? tt("upload.inline.needFolder.retryMissing",
            { missing: missingText, stem: entry.companion.stem || "" })
        : tt("upload.inline.needFolder.retry",
            { stem: entry.companion.stem || "" }));
    }
    return lines.join(" · ");
  }

  /// noteText 约定：**已渲染的文案**（调用方用 t/tt + 形参渲染），或空串。
  /// 不在这里二次 tt——那会把已经渲染好的字节串当键去查表。
  function inlineRowSetStage(entry, key, frac, noteText, indeterminate) {
    var rec = inlineRowEls[entry.id];
    if (!rec) return;
    var resolved = (noteText && typeof noteText === "object" &&
                    typeof noteText.__i18n === "string")
      ? tt(noteText.__i18n) : noteText;
    var extra = resolved ? " · " + resolved : "";
    rec.row.setStage(key, frac, extra, indeterminate);
    if (rec.note && rec.note.textContent !== undefined) {
      rec.note.textContent = inlineRowNoteText(entry);
    }
  }

  function inlineRowMarkError(entry) {
    var rec = inlineRowEls[entry.id];
    if (rec) rec.row.markError();
  }

  function inlineRowRemove(entry) {
    var rec = inlineRowEls[entry.id];
    if (!rec) return;
    if (rec.row._row && rec.row._row.parentNode &&
        rec.row._row.parentNode.removeChild) {
      rec.row._row.parentNode.removeChild(rec.row._row);
    }
    delete inlineRowEls[entry.id];
  }

  /// 终态 / 恢复态按钮（§6.3 的完整表；每种只挂一次，按 state 重算）
  function inlineRowButtons(entry) {
    var rec = inlineRowEls[entry.id];
    if (!rec || !rec.btnHost || !rec.btnHost.appendChild) return;
    var st = entry.state;
    var kinds = inlineButtonKinds(entry);
    var key = kinds.join("|");
    if (rec.renderedFor === key) return;
    rec.renderedFor = key;
    rec.btnHost.textContent = "";
    kinds.forEach(function (kind) {
      if (kind === "support") {
        var sup = document.createElement("button");
        sup.type = "button";
        sup.className = "upload-item-btn";
        sup.textContent = tt("upload.inline.action.requestSupport");
        sup.addEventListener("click", function (e) {
          if (e && e.preventDefault) e.preventDefault();
          openSupportForEntry(entry);
        });
        rec.btnHost.appendChild(sup);
        return;
      }
      if (kind === "saveLocal") {
        var sv = document.createElement("a");
        sv.className = "upload-item-btn";
        sv.href = TOOLS_PAGE_URL;
        sv.target = "_blank";
        sv.rel = "noopener";
        sv.textContent = tt("upload.inline.action.saveLocal");
        rec.btnHost.appendChild(sv);
        return;
      }
      var b = document.createElement("button");
      b.type = "button";
      // 主动作（MRXS 协助的「选择「{stem}」文件夹」与确认态的「使用此文件夹」）
      // 用 accent 实底，与 .btn.primary 同语义；其余保持白底描边
      b.className = kind === "chooseFolder" || kind === "companionUse"
        ? "upload-item-btn is-primary" : "upload-item-btn";
      var label = inlineButtonLabel(kind, entry);
      b.textContent = label;
      b.addEventListener("click", function (e) {
        if (e && e.preventDefault) e.preventDefault();
        inlineAction(entry, kind);
      });
      rec.btnHost.appendChild(b);
    });
  }

  /// 按钮全集（唯一权威表）：按 entry 状态给出动作序列。
  function inlineButtonKinds(entry) {
    var st = entry.state;
    var out = [];
    // MRXS 协助（2026-10）：等待补选同名数据文件夹——主按钮带切片名的
    // 「选择「{stem}」文件夹」，另给重新选择文件与取消（取消 = 移除该行）
    if (st === "need_folder") {
      // 目录选择器与 webkitdirectory 都没有 → 不给死按钮（附注已说明）
      if (folderPickSupported()) out.push("chooseFolder");
      out.push("reselect", "cancel");
      return out;
    }
    if (st === "companion_confirm") {
      out.push("companionUse", "companionReselect");
      return out;
    }
    if (st === "convert_queued" || st === "requeue_convert") {
      if (entry.viaRestore) out.push("continue");
    }
    if (st === "upload_queued" && entry.viaRestore) out.push("continueUpload");
    // 等上传槽的普通行（另一标签正持有跨标签槽）：给取消入口——否则该行
    // 只能靠关闭页面终止（设计 §5 的「排队等待」必须有出路）。
    if (st === "upload_queued" && !entry.viaRestore) out.push("cancel");
    if (st === "need_disk_confirm") out.push("confirmDisk", "cancel");
    // 归属不符（独立复核 #2）：只有显式改绑（工具页 chooseAccountAction
    // 的 'separate' 同语义）与删除本地产物——绝无「继续」。
    if (st === "account_mismatch") out.push("rebindAccount", "discard");
    // 复核 #5：身份确认中的行（auth/info 失败重试期间）——手动「重试」
    // 与取消；绝不静默开始任何会写归属的动作。
    if (st === "identity_wait") out.push("retryIdentity", "cancel");
    if (st === "converting") out.push("cancel");
    // uploading 态的「取消」**按来源分派**，避免行上出现两个同名按钮：
    //   - 直传条目（无 jobId）：uploadFileCos 自己 addRowButton 挂一个，
    //     它走引擎 cancel()（真 abort + POST /cancel）——面板不再重复渲染；
    //   - 产物条目（有 jobId）：走面板 cancel 通道（e._upload / cancelArtifact），
    //     引擎不会为它挂按钮，必须由面板提供。
    if (st === "uploading" && entry.jobId) out.push("cancel");
    if (st === "rejected") out.push("support");
    if (st === "convert_failed") {
      if (entry.error && entry.error.code === "need_bundle") {
        out.push("chooseFolder");
      } else if (entry.companion && entry.companionRetry) {
        // MRXS 协助装配后 planner 报包不完整：主出口是重新选择文件夹
        //（无需重选 .mrxs；成功后原地重新转整包）。彻底选不了文件夹的
        // 浏览器不给死按钮 → 申请支持/保存到本机
        if (folderPickSupported()) out.push("chooseFolder");
        else out.push("support", "saveLocal");
      } else if (entry.error && (entry.error.code === "resume_refused" ||
                                 entry.error.code === "job_dir_missing" ||
                                 entry.error.code === "io_recoverable")) {
        // 设计 §6.3：失败记录的续跑前提已不成立（版本/设置不符、目录被驱逐、
        // 本地读写错误）→ 必须**重新选择文件**才有出路；另给申请支持。
        out.push("reselect", "support", "saveLocal");
      } else {
        out.push("support", "saveLocal");
      }
      if (entry.jobId) out.push("discard");
    }
    if (st === "upload_failed") {
      // P2 问题 2（复核 2026-10-20）：恢复动作按**失败类型 + 实际服务器任务**
      // 决定（uploadRecoveryKinds 是唯一权威表）——分块/网络失败给同任务
      // 续传，状态轮询失败只重查同一任务，明确服务器终态给显式「重新上传」，
      // 格式失败才给「申请支持该格式」。不再按「有没有本机转换 jobId」判定
      //（直传条目本来就没有 jobId，旧判定把它们的恢复入口全部漏成支持申请）。
      var recKinds = window.HP_INLINE_CONVERT &&
        window.HP_INLINE_CONVERT.uploadRecoveryKinds ?
        window.HP_INLINE_CONVERT.uploadRecoveryKinds(entry) : [];
      recKinds.forEach(function (k) {
        if (out.indexOf(k) < 0) out.push(k);
      });
    }
    // 终态（含 `done`——`isTerminal` 已把它计入）：本地产物仍在 OPFS，这是
    // 唯一的清理入口（§6.3）。**不得**再单列 `done` 分支——那会把
    // 「删除本地产物」重复渲染两次（同 kinds 序列里出现两个 discard；
    // `done` 既属终态集又被单列，实测行上出现两个同名按钮）。文案已由
    // `inlineButtonLabel` 按 `uploadState==='published'` 区分「已上传的
    // 平台切片不受影响」。
    if (window.HP_INLINE_CONVERT && window.HP_INLINE_CONVERT.isTerminal(st) &&
        out.indexOf("discard") < 0) {
      if (entry.jobId) out.push("discard");
    }
    return out;
  }

  function inlineButtonLabel(kind, entry) {
    if (kind === "chooseFolder") {
      // MRXS 协助行：按钮点名切片（「选择「病例A」文件夹」）；旧的
      // need_bundle 行 / 整包兜底保持通用文案
      if (entry && entry.companion && entry.companion.stem) {
        return tt("upload.inline.action.chooseFolderNamed",
          { stem: entry.companion.stem });
      }
      return tt("upload.inline.action.chooseFolder");
    }
    if (kind === "companionUse") return tt("upload.inline.companion.use");
    // 确认态的「重新选择」回的是**文件夹**选择，不是文件（复核 r3 C）
    if (kind === "companionReselect") {
      return tt("upload.inline.action.reselectFolder");
    }
    if (kind === "continue") return tt("upload.inline.action.continue");
    if (kind === "continueUpload") return tt("upload.inline.action.continueUpload");
    if (kind === "recheckUpload") return tt("upload.cos.recheck");
    if (kind === "reupload") return tt("upload.inline.action.reupload");
    if (kind === "confirmDisk") return tt("upload.inline.disk.confirm");
    if (kind === "rebindAccount") return tt("upload.inline.action.rebindAccount");
    if (kind === "retryIdentity") return tt("upload.inline.identity.retry");
    if (kind === "cancel") return tt("upload.cos.cancel");
    if (kind === "reselect") return tt("upload.inline.action.reselect");
    if (kind === "discard") {
      var base = (entry.uploadState === "published")
        ? tt("upload.inline.action.discard.published")
        : tt("upload.inline.action.discard");
      // 释放字节数（源副本 + 产物；设计 §6.3）
      var freed = (Number(entry.size) || 0) + (Number(entry.artifactSize) || 0);
      if (entry.jobId && freed > 0) {
        base += " · " + tt("upload.inline.freed", { bytes: humanSize(freed) });
      }
      return base;
    }
    return tt("upload.inline.action.continue");
  }

  function inlineAction(entry, kind) {
    if (!inlineOrch) return;
    if (kind === "chooseFolder") {
      if (entry && entry.companion) {
        // MRXS 协助行：目录选择器（startIn 定位）/ webkitdirectory 回退，
        // 选完经 resolveCompanionFolder 判定/装配——不重选 .mrxs
        void companionPickForEntry(entry);
        return;
      }
      if (els.importFolderInput) els.importFolderInput.click();
      return;
    }
    if (kind === "companionUse") {
      inlineOrch.act(entry.id, "companionUse");
      return;
    }
    if (kind === "companionReselect") {
      inlineOrch.act(entry.id, "companionReselect");
      return;
    }
    if (kind === "support") {
      openSupportForEntry(entry);
      return;
    }
    if (kind === "continueUpload") {
      inlineOrch.act(entry.id, "continue");
      return;
    }
    if (kind === "recheckUpload") {
      // P2 问题 2：状态轮询失败 → 只重查同一任务（同 job 重入状态机，
      // 绝不重建任务/重传分片）
      inlineOrch.act(entry.id, "recheckUpload");
      return;
    }
    if (kind === "reupload") {
      // P2 问题 2：服务器终态 → 显式新建任务（act 内清掉旧任务 id；
      // 只由点击触发，绝不自动重建）
      inlineOrch.act(entry.id, "reupload");
      return;
    }
    if (kind === "discard") {
      inlineOrch.act(entry.id, "discard");
      return;
    }
    if (kind === "cancel") { inlineOrch.act(entry.id, "cancel"); return; }
    if (kind === "confirmDisk") { inlineOrch.act(entry.id, "confirmDisk"); return; }
    if (kind === "rebindAccount") { inlineOrch.act(entry.id, "rebind"); return; }
    if (kind === "retryIdentity") {
      inlineOrch.act(entry.id, "retryIdentity");
      return;
    }
    if (kind === "reselect") {
      // 「重新选择文件」：复用同一个隐藏 file input；选中的文件作为**新条目**
      // 入队（旧条目留在行上供清理，绝不静默替换用户看不到的东西）。
      if (els.fileInput) els.fileInput.click();
      return;
    }
    inlineOrch.act(entry.id, "continue");
  }

  // Failed uploads open a reviewable evidence form; a click never submits a bare request.
  var formatRequestEntry = null;
  function openSupportForEntry(entry) {
    if (!els.formatReqForm || (els.frSubmit && els.frSubmit.disabled)) return;
    var pre = window.HP_INLINE_CONVERT
      ? window.HP_INLINE_CONVERT.prefillFormatRequest(entry, {
          inlineConvert: inlineConvertCaps(),
        }) : { format_ext: "", message: "" };
    formatRequestEntry = entry || null;
    els.frExt.value = pre.format_ext;
    els.frMessage.value = pre.message;
    els.frManufacturer.value = "";
    els.frSampleUrl.value = "";
    els.frSampleAccessCode.value = "";
    els.frSample.value = "";
    // Reuse the selected original when the browser supports FileList assignment.
    // Oversize/bundle inputs require a complete downloadable file/package instead.
    if (entry && !entry.bundle && entry.file && entry.file.size > 0 &&
        entry.file.size <= importDrawerState.maxSampleBytes) {
      try {
        var transfer = new DataTransfer();
        transfer.items.add(entry.file);
        els.frSample.files = transfer.files;
      } catch (e) { /* The visible file picker remains available. */ }
    }
    if (els.frResult) els.frResult.hidden = true;
    openImportDrawer();
    switchImportTab("local");
    els.formatReqForm.hidden = false;
    if (els.formatReqForm.scrollIntoView) els.formatReqForm.scrollIntoView({ block: "nearest" });
    if (els.frManufacturer.focus) els.frManufacturer.focus();
  }

  /// 只重画某行的附注（不触碰阶段/进度——申请支持与转换阶段无关）。
  function inlineRowRefreshNote(entry) {
    var rec = inlineRowEls[entry.id];
    if (!rec || !rec.note || rec.note.textContent === undefined) return;
    rec.note.textContent = inlineRowNoteText(entry);
  }

  /// 上传产物（转换条目：由 onArtifact 提供后走既有 uploadFileCos 语义）。
  /// 面板自己不做传输：把 source 交给共享引擎 createUpload，工作台侧的
  /// 记录适配与直传完全一致。
  function uploadEntryArtifact(entry, artifact) {
    // 取消边界（独立复核 #3 问题 1）：编排器的取消（orch.cancel →
    // entry.cancelled）可能落在下面任何一个异步窗口里（身份等待、getJob
    // 读取、直传内容锁等待）。已取消的条目绝不新建 ingestion——各入口就
    // 地早返回 {cancelled:true}，编排器 completeUpload 据此收口（槽位/锁
    // 照常释放、行保持「已取消」、不弹失败 toast）。
    if (entry.cancelled) return Promise.resolve({ cancelled: true });
    if (!artifact) {
      // 直传条目（无本机产物）：复用既有直传状态机 uploadFileCos——它的
      // 门禁（能力/账号/源大小/词表）、记录持久化、跨标签内容锁与心跳全部
      // 保留；结果经 onOutcome 交回编排器收口（设计 §5.1 直传口径不变）。
      // isCancelled：内容锁等待期间被取消 → uploadFileCos 不建引擎、不 POST，
      // 直接以取消回执收口（与行按钮的 lockAbort 同一窗口的编排器侧覆盖）。
      // 复核 #4：建引擎（记录持久化的 upload.account、跨标签内容锁名的
      // 账号段都按身份写入）之前等身份落定——未知身份绝不写空归属；落定
      // 后仍为空（免认证部署）才按空串继续（免认证本就无归属语义）。
      return authInfoReady().then(function () {
        if (entry.cancelled) return { cancelled: true };
        return new Promise(function (resolve, reject) {
          uploadFileCos(entry.file, inlineEntryRow(entry), {
            directClass: entry.directClass,
            assocTarget: entry.target,
            isCancelled: function () { return !!entry.cancelled; },
            // P2 问题 2（复核 2026-10-20）：恢复语义按**失败类型 + 实际服务
            // 器任务记录**决定，不按是否存在本机转换 jobId。直传条目在这里
            // 把服务器任务 id 挂到条目上（entry.uploadJobId），失败行的
            // 「继续上传/重新检查」据此以 resumeJobId 复用同一任务——绝不
            // 重复建任务、绝不重传已确认分块；「重新上传」（显式终态恢复）
            // 才清掉任务 id 新建。
            onJobCreated: function (id) {
              entry.uploadJobId = id;
              entry._freshUpload = false;
            },
            // 同任务续传：上一轮失败留下的任务 id（part/network/recheck 恢复）；
            // 显式「重新上传」已置 _freshUpload → 不带 resumeJobId（新建任务）。
            resumeJobId: entry._freshUpload ? null : (entry.uploadJobId || null),
            skipConfirm: !!entry.uploadJobId && !entry._freshUpload,
            onEngine: function (upload) {
              // 取消按钮据此真正停传（设计 §5 / §6.3 的「取消」语义）
              entry._upload = upload;
            },
            onOutcome: function (body, err) {
              entry._upload = null;
              if (err && err.__cancelled) resolve({ cancelled: true });
              else if (err) reject(err);
              else resolve({ body: body || {} });
            },
          });
        });
      });
    }
    var name = entry.artifactName || entry.name;
    var declaration = window.HP_INLINE_CONVERT
      ? window.HP_INLINE_CONVERT.artifactDeclaration(entry.artifactCls) : null;
    var cfg = resolveCosConfig();
    if (!cfg) return Promise.reject({ data: { code: "cos_unavailable" } });
    // 复核 #4：建引擎之前等身份落定——createEntryUpload 的 storage.save
    // （upload.account）、引擎 opts.account、getJob 后的归属复核 `me` 都以
    // 真实身份执行；身份未知绝不写空归属（那等于留下「无归属证据」的记录，
    // 之后换账号可绕过归属闸无确认续传）。落定后仍为空（免认证部署）才按
    // 空串继续（免认证本就无归属语义）。
    // 续传语义（设计 §6.4）：先读回 record.upload，再决定续传还是新建。
    // 归属复核（独立复核 #2 的上传入口）：记录的进行中 upload / pending
    // intent 属于另一账号时，绝不新建 ingestion（那会把产物入库到新账号
    // 名下）也绝不改写原记录——直接拒绝；行上的归属处置（显式改绑 /
    // 删除本地产物）由面板编排器的 account_mismatch 态提供。编排器在
    // runUpload 入口已查过一次，这里是真正 POST 前的最后一道闸。
    return authInfoReady().then(function () {
      // 取消边界：身份等待期间被取消 → 不读记录、不建引擎
      if (entry.cancelled) return { cancelled: true };
      return inlineRunner.getJob(entry.jobId).then(function (job) {
        // 取消边界：这次 getJob 读取挂起期间被取消 → 绝不 createEntryUpload
        // （那会新建/续传 ingestion，行却已是「已取消」）；取消早返回值交回
        // 编排器 completeUpload 收口。
        if (entry.cancelled) return { cancelled: true };
        // 现读身份放在读取**之后**（getJob 可能是慢操作）：归属比较与下面的
        // 冻结都按同一时刻的身份，身份中途刷新不会被旧的缓存值放行。
        var me = String(currentUserId || "");
        var up = job && job.upload;
        var it = job && job.intent;
        var inflight = !!(up && up.ingestionId &&
          up.state !== "published" && up.state !== "failed" &&
          up.state !== "cancelled");
        var owner = null;
        if (inflight && up.account) owner = String(up.account);
        else if (!inflight && it && it.state === "pending" && it.account) {
          owner = String(it.account);
        } else if (authInfoEnabled &&
                   (inflight || (it && it.state === "pending"))) {
          // 复核 #5 纵深防御：启用认证的部署中，进行中/pending 的记录却没
          // 有任何归属账号（旧数据或任何漏网写入）→ 归属未知（空串哨兵，
          // 与编排器 recordOwner 同口径），绝不视为可直接上传。
          owner = "";
        }
        // 记录无绑定时，编排器归属复核冻结的 ownerAccount 仍是本条目归属
        //（空串 = 复核时身份未知，不构成归属证据）
        if (owner === null && typeof entry.ownerAccount === "string" &&
            entry.ownerAccount) {
          owner = entry.ownerAccount;
        }
        if (owner !== null && owner !== me) {
          // __account：与编排器自身的归属检查同一收口（failAccountMismatch
          // → account_mismatch 行态，只认改绑/删除）；owner 空串哨兵由其
          // 映射为「此记录没有归属账号」行注。
          return Promise.reject({
            __account: true, owner: owner,
            data: { code: "account_mismatch" },
          });
        }
        // 归属冻结（P1 复核 2026-10-18）：复核**通过的时刻**把归属记在
        // 条目上（记录绑定账号，全新产物 = 当时已核验的页面身份），本次
        // 上传全程（expected_account 与 upload.account 首次写入）只认这个
        // 值——绝不在此后的异步窗口里现读 currentUserId 兜底。
        entry.uploadAccount = owner !== null ? owner : me;
        return createEntryUpload(entry, artifact, job, name, declaration, cfg);
      }, function () {
        // 取消边界：读取失败的回调同样不得在取消后新建 ingestion
        if (entry.cancelled) return { cancelled: true };
        // 归属记录读不到 = 无从证明这条产物归谁——绝不按现读页面身份新建
        // ingestion（身份中途刷新时正好把别人的产物入库到新账号）。行停
        // upload_failed（「继续上传」可重试：重试重新读记录复核归属）。
        return Promise.reject({ data: { code: "owner_read_failed" } });
      });
    });
  }

  /// 面板 entry → uploadFileCos 期望的 row 接口（复用面板已有的 setStage /
  /// markError 与 addRowButton 的行按钮）。
  function inlineEntryRow(entry) {
    return {
      setStage: function (key, frac, note, indeterminate) {
        inlineRowSetStage(entry, key, frac, note, indeterminate);
      },
      markError: function () { inlineRowMarkError(entry); },
      finish: function () { /* 行的存留由面板队列决定（终态按钮要用行） */ },
      _row: (inlineRowEls[entry.id] && inlineRowEls[entry.id].row._row) || null,
    };
  }

  /// 读回记录的续传判定（ready/exported 产物、非终态 upload → 复用同一 ingestion）：
  /// 未收口的 ingestionId → resumeJobId 复用；终态/无记录 → null（新建）。
  function resumeIdFromRecord(job) {
    var up = job && job.upload;
    if (!up || !up.ingestionId) return null;
    if (up.state === "published" || up.state === "failed" ||
        up.state === "cancelled") return null;
    return up.ingestionId;
  }

  /// 条目在记录里已绑定的账号（P1 cross-account 2026-10-15）：进行中的
  /// upload 属创建它的账号；未收口的 pending intent 同样绑定授权账号。
  /// 无绑定证据 → null（调用方回落当前页面身份）。与编排器的 recordOwner
  ///（static/upload/inline-convert.js）同一口径，只读归属、不判他人。
  function inlineBoundAccount(job) {
    var up = job && job.upload;
    var it = job && job.intent;
    if (up && up.ingestionId && up.state !== "published" &&
        up.state !== "failed" && up.state !== "cancelled" &&
        typeof up.account === "string" && up.account) {
      return String(up.account);
    }
    if (it && it.state === "pending" && typeof it.account === "string" &&
        it.account) {
      return String(it.account);
    }
    return null;
  }

  function createEntryUpload(entry, artifact, job, name, declaration, cfg) {
    // 取消边界：建引擎（随后 createUpload 的链上就是 POST /api/ingestions）
    // 前的最后一道闸。引擎句柄（entry._upload）在下一行同步挂上，此后编排
    // 器的取消经 e._upload.cancel() 停掉引擎内部一切在途/后续请求。
    if (entry.cancelled) return Promise.resolve({ cancelled: true });
    // 跨账号归属绑定（P1 cross-account 2026-10-15）：expected_account 用
    // **条目上冻结的归属**（uploadEntryArtifact 归属复核通过时记下的
    // entry.uploadAccount——记录绑定账号，或复核时刻已核验的页面身份），
    // 不是此刻现读的 currentUserId：身份中途被刷新而记录未重判时，现读值
    // 等于授权把产物静默记到新账号名下。冻结值缺失（理论不可达，防御）
    // 才按记录绑定 → 当前身份的既有次序回落。
    var acct = inlineBoundAccount(job);
    if (acct === null && typeof entry.uploadAccount === "string") {
      acct = entry.uploadAccount;
    }
    if (acct === null) acct = String(currentUserId || "");
    return new Promise(function (resolve, reject) {
      var upload = window.HP_COS_UPLOAD.createUpload({
        source: {
          name: name, size: artifact.size,
          slice: function (s, e) { return artifact.slice(s, e); },
        },
        apiFetch: apiFetch,
        heartbeatFetch: apiFetchNoRedirect,
        config: cfg,
        createBody: declaration ? { direct_class: declaration } : null,
        storage: {
          save: function (rec) {
            if (!rec || !rec.job_id) return undefined;
            return inlineRunner.setJobUpload(entry.jobId, {
              ingestionId: rec.job_id, filename: name, size: artifact.size,
              state: "uploading", confirmedParts: rec.confirmed || [],
              partDigests: rec.digests || {},
              digestScheme: rec.digest_scheme || "",
              account: acct,   // 归属写入与 expected_account 同一账号
            });
          },
          complete: function (id, outcome) {
            return inlineRunner.setJobUpload(entry.jobId, {
              ingestionId: id, filename: name, size: artifact.size,
              state: (outcome && outcome.succeeded) ? "published" : "failed",
              slideId: (outcome && outcome.slide_id) || null,
              error: (outcome && outcome.fail_code) || null,
              errorDetail: (outcome && outcome.fail_detail) || null,
            });
          },
          remove: function (id) {
            return inlineRunner.setJobUpload(entry.jobId, {
              ingestionId: id, filename: name, size: artifact.size,
              state: "cancelled",
            });
          },
          readConfirmed: function (id) {
            return inlineRunner.getJob(entry.jobId).then(function (j) {
              var up = j && j.upload;
              return (up && up.ingestionId === id) ? (up.confirmedParts || []) : [];
            });
          },
          readRecord: function (id) {
            return inlineRunner.getJob(entry.jobId).then(function (j) {
              var up = j && j.upload;
              if (!up || up.ingestionId !== id) return null;
              return { confirmed: up.confirmedParts || [],
                       digests: up.partDigests || null,
                       digest_scheme: up.digestScheme || "",
                       account: typeof up.account === "string" ? up.account : "" };
            });
          },
        },
        account: acct,
        resumeJobId: resumeIdFromRecord(job),
        skipConfirm: true,
        confirmResume: function () { return true; },
        retryCompleteOnNetworkError: false,
        useResumeEndpoint: false,
        onEvent: function (ev) {
          if (ev.type === "progress") {
            // 字节副文必须**先渲染**（key + 形参）再交给行：把裸键交给
            // inlineRowSetStage 会显示「已传输 {done} / {total}」占位
            // （与既有 cosProgressNote 同一口径）。
            var bytesNote = "";
            if (ev.determinate !== false &&
                typeof ev.loadedBytes === "number" &&
                typeof ev.totalBytes === "number" && ev.totalBytes > 0) {
              bytesNote = t("upload.cos.bytes", {
                done: humanSize(ev.loadedBytes), total: humanSize(ev.totalBytes),
              });
            } else if (ev.sentAll) {
              bytesNote = t("upload.cos.sent_all");
            }
            inlineRowSetStage(entry, "upload.cos.stage.uploading",
              ev.determinate === false ? null : ev.frac,
              bytesNote, ev.determinate === false);
          } else if (ev.type === "status" && ev.body) {
            // P2 复核 2026-10-20（其他差异 1）：转换产物与直传**共用同一个
            // 服务端阶段映射**（cosStageView）——有真实分母（服务器接收
            // checkpoint）才显示百分比，否则不定态活动指示；不再丢 frac/
            // indeterminate，也不把接收字节百分比冒充完整处理进度。
            var sv = cosStageView(ev.body);
            inlineRowSetStage(entry, sv.key, sv.frac, sv.note, sv.indeterminate);
          } else if (ev.type === "created") {
            entry.uploadJobId = ev.jobId;
          }
        },
      });
      entry._upload = upload;
      upload.done.then(resolve, function (err) {
        if (isAccountMismatchError(err)) {
          // 编排器按服务端核对收口 account_mismatch（改绑/删除）：带上本次
          // 创建声明并冻结的绑定账号——它就是「归属账号」的证据。
          err.expectedAccount = acct;
        }
        reject(err);
      });
    });
  }

  /// 完成收口（§3.5）：刷新列表 → 按**入队时冻结的**目标快照关联 → 打开。
  // ---------- 转换器页「保存到我的项目」：待保存标记与提示（复核 r2） ----

  var PENDING_SAVE_KEY = "pt.tools.pendingSave.v1";
  var PENDING_SAVE_TTL = 24 * 60 * 60 * 1000;

  function pendingSaveRead() {
    try {
      var raw = window.localStorage.getItem(PENDING_SAVE_KEY);
      if (!raw) return null;
      var v = JSON.parse(raw);
      if (!v || !v.jobId || !v.at) return null;
      if (Date.now() - v.at > PENDING_SAVE_TTL) {
        window.localStorage.removeItem(PENDING_SAVE_KEY);
        return null;
      }
      return v;
    } catch (e) { return null; }
  }

  function pendingSaveClear() {
    try { window.localStorage.removeItem(PENDING_SAVE_KEY); } catch (e) { /* */ }
  }

  /// 非阻塞提示条（顶栏下方居中）：消息 + 动作按钮；点任一按钮即移除。
  function showSaveHandoffNotice(message, actions) {
    var host = document.getElementById("save-handoff-notice");
    if (!host) {
      host = document.createElement("div");
      host.id = "save-handoff-notice";
      host.setAttribute("role", "status");
      if (document.body && document.body.appendChild) {
        document.body.appendChild(host);
      }
    }
    host.replaceChildren();
    var p = document.createElement("p");
    p.textContent = message;
    host.appendChild(p);
    var bar = document.createElement("div");
    bar.className = "save-handoff-actions";
    (actions || []).forEach(function (a) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = a.kind === "primary" ? "btn small primary" : "btn small secondary";
      b.textContent = a.label;
      b.addEventListener("click", function () {
        if (host.parentNode) host.parentNode.removeChild(host);
        if (a.onClick) a.onClick();
      });
      bar.appendChild(b);
    });
    host.appendChild(bar);
  }

  /// 待保存标记检查（/app 登录后、restore 之后调用）：标记新鲜且任务仍在
  /// 本机 OPFS → 非阻塞提示「继续保存 / 忽略」；任务已消失 → 静默清标记
  ///（复核 r2 #1：注册/登录绕行后不再丢失待保存任务）。
  function checkPendingSave() {
    var v = pendingSaveRead();
    if (!v) return;
    var finish = function () { pendingSaveClear(); };
    var go = function () {
      pendingSaveClear();
      window.location.href = "/tools/slides?save=" +
        encodeURIComponent(v.jobId);
    };
    var runnerRef = (window.HP_UPLOAD &&
                     typeof window.HP_UPLOAD.inlineRunner === "function")
      ? window.HP_UPLOAD.inlineRunner() : inlineRunner;
    if (!runnerRef || typeof runnerRef.listJobs !== "function") {
      // 运行器不可用（面板内上传关闭等）：无法核对任务——仍提示（继续保存
      // 走转换器页对话框，对话框会显式报告任务是否还在）
      showSaveHandoffNotice(tt("upload.saveproj.notice.text"), [
        { label: tt("upload.saveproj.notice.continue"), onClick: go, kind: "primary" },
        { label: tt("upload.saveproj.notice.dismiss"), onClick: finish },
      ]);
      return;
    }
    runnerRef.listJobs().then(function (jobs) {
      var stillThere = (jobs || []).some(function (j) { return j.id === v.jobId; });
      if (!stillThere) { pendingSaveClear(); return; }   // 任务已消失：静默清
      showSaveHandoffNotice(tt("upload.saveproj.notice.text"), [
        { label: tt("upload.saveproj.notice.continue"), onClick: go, kind: "primary" },
        { label: tt("upload.saveproj.notice.dismiss"), onClick: finish },
      ]);
    }, function () { /* 列表读取失败：下次再提示 */ });
  }

  /// 转换器页移交标记（?save-job=<jobId>，2026-10-22）：restore 完成、身份
  /// 已落定后调用一次。只有「产物就绪、等待上传」的恢复行自动续传；其余
  /// 状态（account_mismatch / identity_wait / 转换未完成等）保持原状——
  /// 行上已有对应的处置入口（含既有 expected_account/归属复核 409 保护）。
  /// 标记用完即摘（history.replaceState），刷新/再次进入绝不重复自动跑。
  function resumeHandoffJob(made) {
    var m = null;
    try { m = window.location.search.match(/[?&]save-job=([^&]+)/); } catch (e) { return; }
    var strip = function () {
      try {
        var u = new URL(window.location.href);
        if (!u.searchParams.has("save-job")) return;
        u.searchParams.delete("save-job");
        window.history.replaceState({}, "", u.pathname + u.search + u.hash);
      } catch (e) { /* 摘不掉不致命：自动续传只发生这一次 */ }
    };
    if (!m) return;
    var jobId = "";
    try { jobId = decodeURIComponent(m[1] || ""); } catch (e) { jobId = ""; }
    strip();
    if (!jobId) return;
    // orch 面经 HP_UPLOAD.inlineOrch() 取（测试注入同一入口）；生产里就是
    // 本模块闭包的 inlineOrch
    var orch = (window.HP_UPLOAD &&
                typeof window.HP_UPLOAD.inlineOrch === "function")
      ? window.HP_UPLOAD.inlineOrch() : inlineOrch;
    // 复核 r2 #2：标记无法兑现时**必须**显式说明（绝不静默忽略）。归属
    // 不符/身份确认由行自身解释（此处不再叠加提示）。
    var fail = function (reasonKey) {
      showSaveHandoffNotice(
        tt("upload.saveproj.fail.prefix") + tt(reasonKey),
        [{ label: tt("upload.saveproj.notice.dismiss"), onClick: null }]);
    };
    if (!orch) {
      fail("upload.saveproj.reason.jobMissing");
      return;
    }
    var entry = (made || []).find(function (e) { return e.jobId === jobId; });
    if (!entry) {
      // 该任务没有恢复出行：本机记录可能已被清理，或不是可上传形态
      fail("upload.saveproj.reason.jobMissing");
      return;
    }
    if (entry.state === "upload_queued") {
      orch.act(entry.id, "continue");
      return;
    }
    if (entry.state === "convert_queued") {
      fail("upload.saveproj.reason.notReady");
      return;
    }
    if (entry.state === "done") {
      fail("upload.saveproj.reason.alreadyUploaded");
      return;
    }
    // 其余（account_mismatch / identity_wait / convert_failed 等）：行上
    // 已有对应的处置入口与说明，不再叠加提示
  }

  function inlineUploaded(entry, body) {
    var sid = entry.slideId || null;
    var name = entry.artifactName || entry.name;
    loadAll();
    importAssociateUploaded(sid || null, name, entry.target || null, null);
    if (sid) openSlide(sid);
    applyBeforeunload();
  }

  // ---------- beforeunload（§6.2 第 7 点） ----------
  // 惰性注册：只在**确有**需要提醒的 entry 时才挂监听（IIFE 期零副作用——
  // 极简 window stub 的测试 harness 没有 addEventListener）。
  var inlineUnloadOn = false;
  var inlineUnloadBound = false;
  function applyBeforeunload() {
    var need = !!(inlineOrch && inlineOrch.warnNeeded && inlineOrch.warnNeeded());
    inlineUnloadOn = need;
    if (need && !inlineUnloadBound &&
        typeof window.addEventListener === "function") {
      inlineUnloadBound = true;
      window.addEventListener("beforeunload", function (ev) {
        if (inlineUnloadOn) {
          ev.preventDefault();
          ev.returnValue = "";
        }
      });
    }
  }

  // 供测试（tests/js/*.test.ts loadApp harness）驱动真实上传路径；
  // 与 HP_AUTH 同风格的命名空间导出，不进业务调用面
  window.HP_UPLOAD = {
    uploadFile: uploadFile,
    // W4/R5：转换轮询（观察超时不判失败；401/403/404 分级处理）
    pollConversionJob: pollConversionJob,
    // COS 直传（测试入口：真实路由/状态机/独立传输的驱动面）
    cosUploadEligible: cosUploadEligible,
    cosIneligibleReason: cosIneligibleReason,
    uploadFileCos: uploadFileCos,
    resolveCosConfig: resolveCosConfig,
    cosStageKey: cosStageKey,
    // upload-liveness：稳定机器码/fail_detail → 可读文案（测试驱动面）
    cosErrorMessage: cosErrorMessage,
    // 存储配额：字节 → GB 文本（测试驱动面）
    storageGbText: storageGbText,
    initCosUploadUi: initCosUploadUi,
    restoreCosJobs: restoreCosJobs,
    // R1 一键转换并上传（工作台入口的驱动面）；转换器交接已移除（2026-10-20）
    browserConvertFormats: browserConvertFormats,
    isBrowserConvertFile: isBrowserConvertFile,
    convertNeededUnavailable: convertNeededUnavailable,
    // 分类分流（测试驱动面；转换器交接已于 2026-10-20 移除）
    uploadFileClassified: uploadFileClassified,
    importDroppedDirectory: importDroppedDirectory,
    // 上传面板内转换（docs/upload-inline-convert-design-20261015.md §2.2⑦；
    // 测试驱动面——面板本体在 window.HP_INLINE_CONVERT）
    inlineEnabled: inlineEnabled,
    inlineConvertCaps: inlineConvertCaps,
    ensureInlineOrch: ensureInlineOrch,
    inlineOrch: function () { return inlineOrch; },
    inlineRunner: function () { return inlineRunner; },
    inlineButtonKinds: inlineButtonKinds,
    inlineButtonLabel: inlineButtonLabel,
    // 转换器「保存到我的项目」移交标记（?save-job=）的处理（测试驱动面）
    resumeHandoffJob: resumeHandoffJob,
    pendingSaveCheck: checkPendingSave,
    pendingSaveRead: pendingSaveRead,
    pendingSaveClear: pendingSaveClear,
    showSaveHandoffNotice: showSaveHandoffNotice,
    // 缺成员清单的行内展示（截断规则；复核 r3 B 的单元测试面）
    missingListText: missingListText,
    inlineRowEls: inlineRowEls,
    inlineWarnNeeded: function () { return inlineUnloadOn; },
    uploadEntryArtifact: uploadEntryArtifact,
    resumeIdFromRecord: resumeIdFromRecord,
    // P1 cross-account：条目记录的绑定账号（expected_account 的来源）
    inlineBoundAccount: inlineBoundAccount,
    legacyUploadFile: legacyUploadFile,
    importBundleRows: importBundleRows,
    submitFormatRequestCore: submitFormatRequestCore,
    openSupportForEntry: openSupportForEntry,
    submitFormatRequest: submitFormatRequest,
  };
  // 供测试（升级 A）：侧栏开合控制器与偏好存取的真实逻辑入口
  window.HP_SIDEBAR = {
    createSidebarController: createSidebarController,
    sidebarPrefKey: sidebarPrefKey,
    parseSidebarPref: parseSidebarPref,
    readSidebarPref: readSidebarPref,
    writeSidebarPref: writeSidebarPref,
    syncViewerLayoutNow: syncViewerLayoutNow,
  };
  // 供测试（改版第四轮 §3）：反馈对话框逻辑入口（payload 形状 / 429 处理）
  window.HP_FEEDBACK_UI = {
    open: feedbackOpen,
    close: feedbackClose,
    submit: feedbackSubmit,
    buildPayload: feedbackBuildPayload,
    retryText: feedbackRetryText,
    state: feedbackState,
  };

  // ---------- 拖拽上传（阶段 1：整页拖放 + 文件夹拖入） ----------
  // document 级 dragenter/over/leave/drop（复用 #drop-overlay 与计数逻辑）：
  // 拖到页面任意位置都不打开文档；文件夹拖入经 webkitGetAsEntry 在事件内
  // 同步取 entry（有效窗口限制），再异步遍历——含 .mrxs/.dat 成员的文件夹
  // 按 MRXS 完整包走本机转换交接，普通文件夹按成员逐个 uploadFile。
  function setupDragDrop() {
    var counter = 0;
    function overlayOn() { els.dropOverlay.classList.add("active"); }
    function overlayOff() { counter = 0; els.dropOverlay.classList.remove("active"); }
    document.addEventListener("dragenter", function (e) {
      e.preventDefault(); counter++; overlayOn();
    });
    document.addEventListener("dragover", function (e) {
      e.preventDefault(); e.dataTransfer.dropEffect = "copy";
    });
    document.addEventListener("dragleave", function (e) {
      e.preventDefault(); counter--; if (counter <= 0) overlayOff();
    });
    document.addEventListener("drop", function (e) {
      e.preventDefault(); overlayOff();
      handleWorkbenchDrop(e.dataTransfer);
    });
  }

  /// drop 事件数据分流：目录项（webkitGetAsEntry 只能在事件处理器内同步取）
  /// 优先——目录走遍历；散文件逐个 uploadFile（内部再按嗅探类别分流）。
  /// 散文件同时同步索取 FileSystemFileHandle（getAsFileSystemHandle 同样
  /// 只能在事件内调用）：单个 .mrxs 进入 MRXS 协助流程后，用它把目录选择器
  /// 定位到原文件旁边（仅定位；用户仍需显式确认文件夹）。
  function handleWorkbenchDrop(dt) {
    if (!dt) return;
    var items = dt.items ? Array.from(dt.items) : [];
    var dirEntries = [];
    var handles = [];
    for (var i = 0; i < items.length; i++) {
      try {
        var entry = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry();
        if (entry && entry.isDirectory) dirEntries.push(entry);
      } catch (e) { /* 非 filesystem 条目：按普通文件处理 */ }
      if (items[i].kind === "file" &&
          typeof items[i].getAsFileSystemHandle === "function") {
        try {
          var h = items[i].getAsFileSystemHandle();
          var f = typeof items[i].getAsFile === "function"
            ? items[i].getAsFile() : null;
          if (f && h) handles.push([f, h]);
        } catch (e2) { /* 句柄不可用：只影响 startIn 定位 */ }
      }
    }
    if (dirEntries.length) {
      for (var d = 0; d < dirEntries.length; d++) {
        importDroppedDirectory(dirEntries[d]);
      }
      return;
    }
    var files = dt.files;
    if (files && files.length > 0) {
      for (var f2 = 0; f2 < files.length; f2++) {
        var handle = null;
        for (var h2 = 0; h2 < handles.length; h2++) {
          if (handles[h2][0] === files[f2]) { handle = handles[h2][1]; break; }
        }
        uploadFile(files[f2], handle ? { fileHandle: handle } : undefined);
      }
    }
  }

  /// 拖入目录的遍历（readEntries 每批 ≤100，循环到空批；成员数封顶）。
  /// bundle 成员（`.mrxs`/`.dat`/`.vms`）→ 开关开时进面板队列（整包转换），
  /// 关闭时弹窗交接；普通目录 → 成员文件逐个 uploadFile。无法遍历给明确提示。
  function importDroppedDirectory(dirEntry) {
    var MAX_MEMBERS = 8192;
    function collect(entry, prefix, rows) {
      return new Promise(function (resolve, reject) {
        if (!entry) return resolve();
        if (entry.isFile) {
          return entry.file(function (file) {
            rows.push({
              name: entry.name,
              relPath: prefix ? prefix + "/" + entry.name : entry.name,
              file: file,
            });
            if (rows.length > MAX_MEMBERS) {
              reject(new Error("too_many_members"));
              return;
            }
            resolve();
          }, reject);
        }
        if (!entry.isDirectory) return resolve();
        if (typeof entry.createReader !== "function") {
          return reject(new Error("unreadable"));
        }
        var reader = entry.createReader();
        var readBatch = function () {
          reader.readEntries(function (batch) {
            if (!batch || !batch.length) return resolve();
            var chain = Promise.resolve();
            batch.forEach(function (child) {
              chain = chain.then(function () {
                var dirPath = prefix
                  ? prefix + "/" + entry.name : entry.name;
                return collect(child, dirPath, rows);
              });
            });
            chain.then(readBatch, reject);
          }, reject);
        };
        readBatch();
      });
    }
    var rows = [];
    collect(dirEntry, "", rows).then(function () {
      // bundle 判定（设计 §4.1.1）：正则对齐到 `.mrxs|dat|vms`（**不含 `.vmu`**
      // ——VMU 今天在直传受理集内，划进 bundle 分支会把它从「可用」变成
      // planner 拒绝，属收紧能力）。「是不是完整包 / 缺哪个成员」只由
      // 运行器的 planner 判，这里只做**分流**。
      var members = rows.filter(function (r) {
        return /\.(mrxs|dat|vms)$/i.test(r.name);
      });
      if (members.length) {
        importBundleRows(rows, dirEntry.name);
        return;
      }
      rows.forEach(function (r) { uploadFile(r.file); });
    }).catch(function () {
      toast(tt("imp.drop.dir.unreadable"), "error");
    });
  }

  /// bundle 分流（设计 §2.2⑩b / §4.1.1）：面板可用 → 进面板队列；面板不可用
  ///（开关关闭/引擎初始化失败）→ 行内说明 + 重试，绝不跳转换器页面
  ///（2026-10-20：转换器交接已移除）。
  function importBundleRows(rows, folderName) {
    var retryBundle = function () { importBundleRows(rows, folderName); };
    if (inlineEnabled()) {
      ensureInlineOrch().then(function (orch) {
        if (orch) {
          orch.enqueueBundle(rows, folderName, {
            assocTarget: importTargetSnapshot(),
          });
        } else {
          convertNeededUnavailable(null, folderName, retryBundle);
        }
      });
      return;
    }
    convertNeededUnavailable(null, folderName, retryBundle);
  }

  /// MRXS 协助的 webkitdirectory 回退：目录选择器不可用/被拒时，行上的
  /// 「选择文件夹」改点这个隐藏 input；一次待处理的协助（entry id + stem）。
  var pendingCompanion = null;

  /// 这个浏览器里还能不能选文件夹：showDirectoryPicker 或 webkitdirectory
  /// 任一可用即可；两者都没有时 need_folder 行不给「选择文件夹」死按钮
  ///（附注改说明不可用）。协助模块缺失 → 不臆测（保持旧行为）。
  function folderPickSupported() {
    var C = window.HP_MRXS_COMPANION;
    if (!C) return true;
    if (typeof C.supportsDirectoryPicker === "function" &&
        C.supportsDirectoryPicker()) return true;
    if (typeof C.supportsFolderPicker === "function") {
      return !!C.supportsFolderPicker();
    }
    return true;
  }

  /// 缺成员清单的行内展示（复核 r3 B）：全部列出；超过 5 个时只列前 5 个
  /// 并追加「等 N 个文件」（zh）/ ", and N more"（en）。分隔符随语言。
  function missingListText(missing) {
    var list = (Array.isArray(missing) ? missing : [])
      .filter(function (m) { return !!m; })
      .map(function (m) { return String(m); });
    if (!list.length) return "";
    var sep = (window.HP_I18N && typeof window.HP_I18N.getLang === "function" &&
               window.HP_I18N.getLang() === "en") ? ", " : "、";
    var MAX = 5;
    if (list.length <= MAX) return list.join(sep);
    var names = list.slice(0, MAX).join(sep);
    return tt("upload.inline.needFolder.missingMore",
      { names: names, n: String(list.length) });
  }

  /// 「选择文件夹（MRXS）」（webkitdirectory input）→ 与拖入目录同一交接。
  /// 协助流程待处理时（pendingCompanion）改道：这次选择是给那个 .mrxs 行
  /// 补选同名数据文件夹，交给 resolveCompanionFolder 判定/装配。
  function importPickFolder() {
    var list = els.importFolderInput && els.importFolderInput.files;
    var files = list ? Array.from(list) : [];
    if (!files.length) return;
    var rows = files.map(function (f) {
      return {
        name: f.name,
        relPath: f.webkitRelativePath || f.name,
        file: f,
      };
    });
    var first = rows[0].relPath.split("/")[0] || rows[0].name;
    els.importFolderInput.value = "";   // 允许再次选择同一目录触发 change
    if (pendingCompanion && inlineOrch) {
      var pc = pendingCompanion;
      pendingCompanion = null;
      inlineOrch.resolveCompanionFolder(pc.id, rows, first);
      return;
    }
    // 与拖入目录同一分流（设计 §2.2⑩b）：开关开 → 面板队列；关闭 → 交接
    importBundleRows(rows, first);
  }

  /// 协助行的「选择「{stem}」文件夹」：目录选择器优先（startIn 定位到原
  /// .mrxs 旁边）；无 API/SecurityError → 回退 webkitdirectory input（同
  /// 手势内 click）。判定/装配在 orchestrator.resolveCompanionFolder。
  async function companionPickForEntry(entry) {
    var C = window.HP_MRXS_COMPANION;
    var comp = entry && entry.companion ? entry.companion : {};
    var stem = comp.stem || "";
    if (!C || typeof C.pickDirectory !== "function" ||
        !C.supportsDirectoryPicker()) {
      pendingCompanion = { id: entry.id, stem: stem };
      if (els.importFolderInput) els.importFolderInput.click();
      return;
    }
    var startIn = null;
    if (comp.handle) {
      try { startIn = await comp.handle; } catch (e) { startIn = null; }
    }
    var res = await C.pickDirectory({ id: "mrxs-companion", startIn });
    if (res.ok) {
      var rows = null;
      try {
        // picked 目录不同名时只遍历同名直接子目录（多切片父文件夹不整树遍历）
        rows = await C.collectCompanionFolder(res.handle, stem,
          { maxMembers: 8192 });
      } catch (e2) {
        inlineOrch.resolveCompanionFolder(entry.id, [], res.name,
          { traversalError: true });
        return;
      }
      inlineOrch.resolveCompanionFolder(entry.id, rows,
        res.name || res.handle.name || "");
      return;
    }
    if (res.reason === "cancelled") return;   // 用户取消：安静保持待选态
    pendingCompanion = { id: entry.id, stem: stem };   // security/error → 回退
    if (els.importFolderInput) els.importFolderInput.click();
  }

  // =========================================================================
  // W4/W6（2026-09-14）：统一「导入切片」抽屉 —— 本地文件 / 百度分享两页签、
  // 格式目录（GET /api/slide-formats）、「申请新格式支持」次级入口与
  // 后台任务列表（GET /api/conversions?group=open|recent）。
  // 本地上传仍走既有 legacy/V2 管线（不重写）；抽屉只负责入口、目标位置
  // 与任务可视化。百度页签按能力探测结果降级：不可枚举 → 显示可行动原因；
  // 可枚举不可导入 → 允许读列表、禁用导入。分享文本/提取码只进请求体，
  // 绝不写日志或 console。
  // =========================================================================
  var importDrawerState = {
    open: false,
    tab: "local",            // "local" | "baidu"
    formatsLoaded: false,
    formatCatalog: [],
    maxSampleBytes: 64 * 1024 * 1024,   // /api/slide-formats 下发后覆盖
    taskTimer: null,
    taskPollStopped: false,  // 401/403 后停止（权限失效，重试无意义）
    lastServerTasks: null,   // 最近一次 /api/conversions 合并结果（本机队列立即重画用）
    lastFocusEl: null,
    // folder-experience §1：目标选择会话——每次打开抽屉 generation 前进；
    // 用户操作任一目标下拉/新建名后 dirty=true，迟到的项目列表不得覆盖。
    targetGen: 0,
    targetDirty: false,
  };

  // 本地页签目标位置（抽屉会话内保留）："" = 未归类；"<pid>" = 已有项目；
  // "new" = 新项目（首个上传成功时懒创建一次，幂等键固定，后续复用 pid）。
  var importTargetState = {
    pid: "",
    newProjectName: "",
    createdPid: "",
    createKey: null,
  };
  // 可观测镜像（W4 spec 命名）：上传成功后据此 POST /api/project/<pid>/slides
  window.__importTargetPid = "";

  // ---------- folder-experience §1：默认目标 / 目标快照 ----------
  // 导入默认目标 = 当前真实文件夹，且仅当它出现在**最新授权项目列表**且服务
  // 端显式标记 can_write 时才默认写入；根目录、「临时查看」集合、目录不在最
  // 新列表、字段缺失（旧后端）或显式 false → 未归类。绝不以「列表里看得到」
  // 推断可写（服务端 /api/projects 逐行下发 can_write，见 api_projects）。
  function importDefaultTargetPid() {
    var key = fbState.folder;
    if (!key || key === FB_TEMP_KEY) return "";
    var p = fbProject(key);
    if (!p || p.can_write !== true) return "";
    return key;
  }

  // 会话默认落位：本地与百度两个下拉同用；dirty（用户已操作）时不覆盖。
  function applyImportTargetDefault() {
    if (importDrawerState.targetDirty) return;
    var def = importDefaultTargetPid();
    if (els.importTargetSelect) els.importTargetSelect.value = def;
    if (els.baiduTargetSelect) els.baiduTargetSelect.value = def;
    syncImportTargetFromSelect();
  }

  // 目标快照：上传开始时冻结——之后侧栏切换目录、抽屉重开、下拉改动都不影
  // 响本次任务的归属；失败重试沿用同一快照（含「新项目」幂等键，绝不重复建
  // 目录、不重传已发布切片、不自动改成当前目录）。
  function importTargetSnapshot() {
    var st = importTargetState;
    if (st.newProjectName && !st.createKey) st.createKey = uuid();
    return {
      pid: st.pid || "",
      newProjectName: st.newProjectName || "",
      createdPid: st.createdPid || "",
      createKey: st.createKey || null,
    };
  }

  function jsonBody(r) {
    return r.json().then(function (b) { return { ok: r.ok, status: r.status, body: b }; },
                           function () { return { ok: r.ok, status: r.status, body: null }; });
  }

  // ---------- 本地页签：目标位置 ----------
  function impOptionEl(value, label) {
    var o = document.createElement("option");
    o.value = value;
    o.textContent = label;
    return o;
  }

  function renderImportTargetSelects() {
    var projects = allProjects || [];
    if (els.importTargetSelect) {
      var keep = els.importTargetSelect.value;
      els.importTargetSelect.innerHTML = "";
      els.importTargetSelect.appendChild(impOptionEl("", t("imp.target.unfiled")));
      projects.forEach(function (p) {
        els.importTargetSelect.appendChild(impOptionEl(p.pid, p.name || p.pid));
      });
      els.importTargetSelect.appendChild(impOptionEl("new", t("imp.target.new")));
      els.importTargetSelect.value = keep || "";
    }
    if (els.baiduTargetSelect) {
      var keepB = els.baiduTargetSelect.value;
      els.baiduTargetSelect.innerHTML = "";
      els.baiduTargetSelect.appendChild(impOptionEl("", t("imp.target.unfiled")));
      projects.forEach(function (p) {
        els.baiduTargetSelect.appendChild(impOptionEl(p.pid, p.name || p.pid));
      });
      els.baiduTargetSelect.value = keepB || "";
    }
  }

  function syncImportTargetFromSelect() {
    if (!els.importTargetSelect) return;
    var v = els.importTargetSelect.value;
    if (els.importTargetNewName) els.importTargetNewName.hidden = v !== "new";
    if (v === "new") {
      importTargetState.pid = "";
      importTargetState.newProjectName = (els.importTargetNewName.value || "").trim();
    } else {
      importTargetState.pid = v || "";
      importTargetState.newProjectName = "";
    }
    window.__importTargetPid = importTargetState.pid;
  }

  // 上传成功后的目标关联（W4）：有目标才动作；目标被删/权限撤销 → 提示
  // 关联失败，产物保留在未归类（不静默改目标）。
  // P2 合同 §3.2：slideId 可得时发 {slide_ids:[...]}；旧后端/缺 id 回落
  // {slides:[名]}。
  // folder-experience §1.3/§1.4：snap = 上传开始时冻结的目标快照（缺省沿用
  // 当前导入会话状态，兼容旧调用方）；失败保留切片与重试入口——行内「重试
  // 加入文件夹」按钮 + 按身份持久化的待办（下次启动自动重试一次原目标），
  // 沿用原 slide ID/回执与原幂等键，绝不重传已发布切片、绝不改成当前目录。
  function importAssociateUploaded(slideId, slideName, snap, row) {
    if (!slideId && !slideName) return Promise.resolve();
    var st = snap || importTargetState;
    if (!st.pid && !st.newProjectName) return Promise.resolve();
    var ensureProject;
    if (st.pid) {
      ensureProject = Promise.resolve(st.pid);
    } else if (st.createdPid) {
      ensureProject = Promise.resolve(st.createdPid);
    } else {
      if (!st.createKey) st.createKey = uuid();
      var projectName = st.newProjectName;
      ensureProject = apiFetch("/api/project/create", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": st.createKey },
        body: JSON.stringify({ name: projectName, note: "", slides: [] }),
      }).then(jsonBody).then(function (res) {
        if (!res.ok || !res.body || !res.body.pid) {
          throw new Error((res.body && res.body.error) || t("newproj.create.fail"));
        }
        st.createdPid = res.body.pid;
        // 同一幂等键的活会话共享建目录产物（快照期间不重复创建）
        if (importTargetState.createKey === st.createKey && !importTargetState.createdPid) {
          importTargetState.createdPid = st.createdPid;
        }
        return st.createdPid;
      });
    }
    return ensureProject.then(function (pid) {
      var payload = slideId ? { slide_ids: [slideId] } : { slides: [slideName] };
      return apiFetch("/api/project/" + encodeURIComponent(pid) + "/slides", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(jsonBody).then(function (res) {
        if (!res.ok) throw new Error((res.body && res.body.error) || t("imp.assoc.fail"));
        toast(t("imp.assoc.ok", { name: truncateMiddle(slideName || slideId, 24) }), "success");
        fbAssocPendingClear(slideId, slideName);
        return reloadProjectsAndUnfiled();
      });
    }).catch(function (e) {
      toast(t("imp.assoc.fail2", { e: (e && e.message) ? e.message : e }), "error");
      fbAssocPendingSave(slideId, slideName, st);
      if (row && row._row && row._row.appendChild && !row.__assocRetry) {
        row.__assocRetry = true;   // 同一行只挂一次重试入口
        addRowButton(row, t("imp.assoc.retry"), function () {
          importAssociateUploaded(slideId, slideName, st, row);
        });
      }
    });
  }

  // ---------- 格式目录（GET /api/slide-formats，打开抽屉时拉一次） ----------
  // Wave 3（普通图片兼容）：目录是格式词表的唯一权威（§3），raster-image 行
  // 与后端 slide_format_registry._CATALOG_DISPLAY 同文；一致性由
  // tests/js/raster-image-compat.test.ts 对后端源码做契约校验，防漂移。
  // 阶段 1（先转换后上传）：import_mode 三值 direct-upload/convert/
  // direct-temporary 与后端目录行同文。
  var FORMAT_CATALOG_FALLBACK = [
    { display_name: "SVS / TIFF / BigTIFF / OME-TIFF / NDPI / VMS / VMU / SCN / BIF / SVSlide",
      extensions: [".svs", ".tif", ".tiff", ".ome.tif", ".ome.tiff", ".ndpi", ".vms", ".vmu", ".scn", ".bif", ".svslide"],
      import_mode: "direct-temporary", limits: [] },
    { id: "raster-image", display_name: "普通图片（BMP / JPEG）",
      extensions: [".bmp", ".jpg", ".jpeg"], import_mode: "convert",
      limits: ["可在本机浏览器转换为 OME-TIFF 后上传（识别未压缩 "
             + "24/32 位 BMP 与三分量基线 JPEG；整图按瓦片重编码，"
             + "无物理标尺）；RLE / 位域 / 调色板位深 BMP 与渐进 / "
             + "灰度 JPEG 走暂时直传；平台当前仍受理 .bmp / .jpg / "
             + ".jpeg 直接导入"] },
    { id: "kfb-kfbf", display_name: "KFB / KFBF", extensions: [".kfb", ".kfbf"],
      import_mode: "convert",
      limits: ["在本机浏览器中转换后上传：KFB 转为 BigTIFF（明场），KFBF 转为多通道 OME-TIFF（荧光）"] },
    { id: "tmap", display_name: "TMAP06（Unic，单文件明场）", extensions: [".tmap"],
      import_mode: "convert",
      limits: ["仅 TMAP06 单文件明场（已验证公开样片）在本机浏览器转换后上传；"
             + "重叠瓦片按绝对坐标拼接后高质量重编码，头部像素尺寸未经标定核验"
             + "（输出不写 µm/px），关联缩略图/标签/宏图不嵌入；原始 .tmap 不直接导入"] },
    { id: "mrxs", display_name: "MRXS", extensions: [".mrxs"], import_mode: "convert",
      limits: ["需要完整包（主文件 + 同名伴随目录）；在工作台选择整个文件夹，在本机浏览器转换后上传"] },
  ];

  // 文件选择器 accept 的静态 fallback：= acceptFromCatalog(FORMAT_CATALOG_FALLBACK)
  // 的展开结果（单一权威链：后端目录 → fallback 目录 → accept → 模板属性）。
  // 契约（两处断言）由 raster-image-compat.test.ts 锁定；接口可用时该串会被
  // 同一函数对线上目录的派生结果覆盖。
  var FILE_INPUT_ACCEPT_FALLBACK = ".svs,.tif,.tiff,.ome.tif,.ome.tiff,.ndpi,.vms,.vmu,.scn,.bif,.svslide,.bmp,.jpg,.jpeg,.kfb,.kfbf,.tmap,.mrxs,.zip";

  // 由格式目录派生文件选择器 accept（Wave 3）：
  //   - 只取 selectable_for_upload !== false 的条目扩展名（小写、去重、保序）；
  //   - .zip 恒定保留：它是 MRXS 完整包的运输容器，不属于任何目录条目扩展名；
  //   - 目录为空/形态异常时返回 null（调用方保留静态 fallback，不缩窄能力）。
  function acceptFromCatalog(items) {
    if (!Array.isArray(items) || items.length === 0) return null;
    var seen = {};
    var parts = [];
    items.forEach(function (f) {
      if (!f || f.selectable_for_upload === false) return;
      (f.extensions || []).forEach(function (ext) {
        var e = String(ext || "").trim().toLowerCase();
        if (!e || e.charAt(0) !== "." || seen[e]) return;
        seen[e] = true;
        parts.push(e);
      });
    });
    if (!parts.length) return null;
    if (!seen[".zip"]) { seen[".zip"] = true; parts.push(".zip"); }
    return parts.join(",");
  }

  // 目录拉取成功后把派生 accept 写回 #file-input（静态属性作为兜底先行存在）
  function applyFileInputAccept(items) {
    if (!els.fileInput) return;
    var a = acceptFromCatalog(items);
    if (a) els.fileInput.accept = a;
  }

  function catalogText(key, fallback) {
    if (!key || /\.(undefined|null)$/.test(key)) return fallback;
    var s = t(key);
    return (s && s !== key) ? s : fallback;
  }

  function formatModeLabel(mode) {
    // 阶段 1 三种徽标：直接上传 / 本机转换后上传 / 暂时直接导入
    if (mode === "direct-upload") return t("imp.formats.mode.direct-upload");
    if (mode === "convert") return t("imp.formats.mode.convert");
    if (mode === "direct-temporary") return t("imp.formats.mode.direct-temporary");
    return t("imp.formats.mode.direct-temporary");
  }

  function renderFormatCatalog(items) {
    if (!els.importFormatCatalog) return;
    els.importFormatCatalog.innerHTML = "";
    (items || []).forEach(function (f) {
      var row = document.createElement("div");
      row.className = "imp-format-row";
      var head = document.createElement("div");
      head.className = "imp-format-head";
      var nm = document.createElement("span");
      nm.className = "imp-format-name";
      nm.textContent = catalogText("imp.formats.name." + f.id, f.display_name || f.id || "") +
        "  " + (f.extensions || []).join(" / ");
      var badge = document.createElement("span");
      badge.className = "imp-format-badge mode-" + (f.import_mode || "direct");
      badge.textContent = formatModeLabel(f.import_mode);
      head.appendChild(nm);
      head.appendChild(badge);
      row.appendChild(head);
      // 目录文案随界面语言：有本地化条目用本地化，否则用服务端原文
      var localLimit = catalogText("imp.formats.limits." + f.id, null);
      (localLimit ? [localLimit] : (f.limits || [])).forEach(function (lim) {
        var l = document.createElement("div");
        l.className = "imp-format-limit";
        l.textContent = lim;
        row.appendChild(l);
      });
      els.importFormatCatalog.appendChild(row);
    });
  }

  function loadImportFormatCatalog() {
    if (!els.importFormatCatalog) return Promise.resolve();
    if (importDrawerState.formatsLoaded) return Promise.resolve();
    els.importFormatCatalog.textContent = t("imp.formats.loading");
    return apiFetch("/api/slide-formats").then(function (resp) {
      // 需要响应头（样本上限下发），故不用 jsonBody——自取 body 与 headers。
      // 兼容两种响应形态：真 Response（有 headers.get）与测试替身（无 headers）。
      var headers = (resp && resp.headers && typeof resp.headers.get === "function")
        ? resp.headers : null;
      return resp.json().then(function (b) {
        return { ok: resp.ok, status: resp.status, body: b, headers: headers };
      }, function () {
        return { ok: resp.ok, status: resp.status, body: null, headers: headers };
      });
    }).then(function (res) {
      var body = res.body;
      var items = body && (body.formats || body.items);
      if (!Array.isArray(items) && Array.isArray(body)) items = body;
      if (!res.ok || !Array.isArray(items)) {
        renderFormatCatalog(FORMAT_CATALOG_FALLBACK);   // 后端暂缺：静态产品文案兜底
        return;
      }
      importDrawerState.formatCatalog = items;
      // 样本上限：优先响应头（服务端 FORMAT_REQUEST_MAX_SAMPLE_BYTES 的
      // 权威下发；响应体是裸列表，没有该字段），其次兼容包封形状
      //（body.max_sample_bytes，旧/替身形态）。两者都缺才保留 64 MiB 初值。
      var maxSample = Number(res.headers &&
                             res.headers.get("X-Format-Request-Max-Sample-Bytes"));
      if (!(maxSample > 0)) maxSample = Number(body && body.max_sample_bytes);
      if (maxSample > 0) importDrawerState.maxSampleBytes = maxSample;
      importDrawerState.formatsLoaded = true;
      renderFormatCatalog(items);
      // Wave 3：文件选择器 accept 由目录派生（selectable_for_upload 条目并集 +
      // .zip），目录不可达时保持模板静态 fallback（含 .bmp/.jpg/.jpeg）
      applyFileInputAccept(items);
      renderFrSampleMax();
    }).catch(function () {
      renderFormatCatalog(FORMAT_CATALOG_FALLBACK);
    });
  }

  function renderFrSampleMax() {
    if (!els.frSampleMax) return;
    els.frSampleMax.textContent = t("fr.sample.max", {
      size: fmtSize(importDrawerState.maxSampleBytes),
    });
  }

  // ---------- 申请新格式支持（次级入口；202 = 已登记，等待评估） ----------
  function frBusinessStatusLabel(status) {
    if (status === "reviewing") return t("fr.status.reviewing");
    if (status === "supported") return t("fr.status.supported");
    if (status === "declined") return t("fr.status.declined");
    return t("fr.status.submitted");   // submitted / 未知一律「已登记，等待评估」
  }

  function renderFormatRequestResult(rec) {
    if (!els.frResult) return;
    els.frResult.innerHTML = "";
    els.frResult.hidden = false;
    var line = document.createElement("span");
    line.textContent = t("fr.ok.registered", { id: (rec && rec.request_id) || (rec && rec.id) || "" }) +
      " · " + frBusinessStatusLabel(rec && rec.business_status);
    els.frResult.appendChild(line);
    var refresh = document.createElement("button");
    refresh.type = "button";
    refresh.className = "link-btn";
    refresh.textContent = t("fr.refresh");
    refresh.addEventListener("click", function () {
      var rid = rec && (rec.request_id || rec.id);
      if (!rid) return;
      apiFetch("/api/format-requests/" + encodeURIComponent(rid))
        .then(jsonBody)
        .then(function (res) {
          if (res.ok && res.body) {
            renderFormatRequestResult(res.body);
            loadRecentFormatRequests();
          } else {
            toast(t("fr.refresh.fail"), "error");
          }
        })
        .catch(function () { toast(t("fr.refresh.fail"), "error"); });
    });
    els.frResult.appendChild(refresh);
  }

  function loadRecentFormatRequests() {
    if (!els.frRecent) return;
    apiFetch("/api/format-requests").then(jsonBody).then(function (res) {
      if (!res.ok || !res.body || !Array.isArray(res.body.items)) {
        els.frRecent.innerHTML = "";   // 接口暂缺/失败：静默（次级入口不打扰）
        return;
      }
      els.frRecent.innerHTML = "";
      if (!res.body.items.length) return;
      var title = document.createElement("div");
      title.className = "fr-recent-title";
      title.textContent = t("fr.recent.title");
      els.frRecent.appendChild(title);
      res.body.items.slice(0, 5).forEach(function (it) {
        var row = document.createElement("div");
        row.className = "fr-recent-row";
        row.textContent = (it.format_ext || "") + " · " + frBusinessStatusLabel(it.business_status);
        els.frRecent.appendChild(row);
      });
    }).catch(function () { /* 静默 */ });
  }

  /// 申请支持内核（设计 §2.2⑧ / §7.1）：抽屉表单与上传行的「申请支持该格式」
  /// 共用同一提交路径（同一端点、同一 202/413/429 语义、同一预检）。
  /// opts: {ext, message, contact, sampleFile, onResult, onError}
  /// 返回 Promise<boolean>（true=已登记）：调用方据此在在途期禁用提交按钮
  /// 并恢复——不返回 Promise 会让「先 disabled=true 再同步 disabled=false」
  /// 变成永不生效的抑制（重复点击即多次 POST，直接吃掉每日额度）。
  function submitFormatRequestCore(opts) {
    opts = opts || {};
    var ext = String(opts.ext || "").trim();
    if (!ext) {
      var need = t("fr.need.ext");
      if (opts.onError) opts.onError(need);
      else toast(need, "error");
      return Promise.resolve(false);
    }
    var sample = opts.sampleFile || null;
    var manufacturer = String(opts.manufacturer || "").trim();
    var sampleUrl = String(opts.sampleUrl || "").trim();
    var accessCode = String(opts.sampleAccessCode || "").trim();
    var errorKey = !manufacturer || manufacturer.length > 500 ? "fr.need.manufacturer" : "";
    if (!errorKey && !sample && !sampleUrl) errorKey = "fr.need.sample";
    if (!errorKey && sample && !sample.size) errorKey = "fr.sample.empty";
    if (!errorKey && accessCode && !sampleUrl) errorKey = "fr.sample.url.invalid";
    if (!errorKey && accessCode.length > 200) errorKey = "fr.sample.code.invalid";
    if (!errorKey && sampleUrl) {
      try {
        var url = new URL(sampleUrl);
        if (sampleUrl.length > 2000 || /[\s\x00-\x1f]/.test(sampleUrl) ||
            !/^https?:$/.test(url.protocol) || !url.hostname || url.username || url.password ||
            url.port === "0") errorKey = "fr.sample.url.invalid";
      } catch (e) { errorKey = "fr.sample.url.invalid"; }
    }
    if (errorKey) {
      if (opts.onError) opts.onError(t(errorKey));
      else toast(t(errorKey), "error");
      return Promise.resolve(false);
    }
    // 前端预检样本大小（服务端仍权威校验）
    if (sample && sample.size > importDrawerState.maxSampleBytes) {
      var tooBig = t("fr.sample.too.large", {
        size: fmtSize(importDrawerState.maxSampleBytes),
      });
      if (opts.onError) opts.onError(tooBig);
      else toast(tooBig, "error");
      return Promise.resolve(false);
    }
    var fd = new FormData();
    fd.append("format_ext", ext);
    fd.append("message", String(opts.message || "").trim());
    fd.append("contact", String(opts.contact || "").trim());
    fd.append("manufacturer", manufacturer);
    fd.append("sample_url", sampleUrl);
    fd.append("sample_access_code", accessCode);
    if (sample) fd.append("sample", sample);
    // 401 不跳转（设计 §7.1）：申请支持不属状态推进请求，不把页面导航走
    return apiFetchNoRedirect("/api/format-requests", { method: "POST", body: fd })
      .then(jsonBody)
      .then(function (res) {
        if (res.status === 202) {
          // 202 只表示登记成功：展示申请编号 + 业务状态（不说「已发送/已兼容」）
          if (opts.onResult) opts.onResult(res.body);
          else renderFormatRequestResult(res.body);
          loadRecentFormatRequests();
          return true;
        }
        // 413/429/其他失败：保留草稿（输入不清空），就地提示。
        // 这两类**不**回显服务端的中文原文——英文界面下也应是英文（§9.2 第 13 项）。
        var msg = frFailureText(res.status, res.body);
        if (opts.onError) opts.onError(msg);
        else toast(msg, "error");
        return false;
      })
      .catch(function () {
        var msg = t("fr.fail");
        if (opts.onError) opts.onError(msg);
        else toast(msg, "error");
        return false;
      });
  }

  /// 服务端失败 → 本地化文案（429 每日上限 / 413 样本超限 / 其余通用；
  /// 未知码保留原始 error 文本供排障）。
  function frFailureText(status, body) {
    var code = (body && body.code) || "";
    var evidenceErrors = { sample_required: "fr.need.sample", sample_empty: "fr.sample.empty",
      manufacturer_required: "fr.need.manufacturer", invalid_sample_url: "fr.sample.url.invalid",
      invalid_sample_access_code: "fr.sample.code.invalid" };
    if (evidenceErrors[code]) return t(evidenceErrors[code]);
    if (status === 429 || code === "rate_limited") return t("fr.err.rate");
    if (status === 413 || code === "request_too_large") return t("fr.err.too_large");
    return (body && body.error) || t("fr.fail");
  }

  function submitFormatRequest() {
    if (!els.frExt || !els.frSubmit || els.frSubmit.disabled) return;
    var ext = (els.frExt.value || "").trim();
    if (!ext) { toast(t("fr.need.ext"), "error"); return; }
    // 前端预检样本大小（服务端仍权威校验）
    if (els.frSample && els.frSample.files && els.frSample.files[0] &&
        els.frSample.files[0].size > importDrawerState.maxSampleBytes) {
      toast(t("fr.sample.too.large", {
        size: fmtSize(importDrawerState.maxSampleBytes),
      }), "error");
      return;
    }
    var entry = formatRequestEntry;
    els.frSubmit.disabled = true;
    submitFormatRequestCore({
      ext: ext,
      message: els.frMessage.value,
      manufacturer: els.frManufacturer.value,
      sampleUrl: els.frSampleUrl.value,
      sampleAccessCode: els.frSampleAccessCode.value,
      contact: els.frContact.value,
      sampleFile: (els.frSample.files && els.frSample.files[0]) || null,
      onResult: function (rec) {
        renderFormatRequestResult(rec);
        if (entry) {
          entry.supportRequestId = rec.request_id || rec.id || "";
          entry.supportBusinessStatus = rec.business_status || "submitted";
          if (entry.id) inlineRowRefreshNote(entry);
        }
        formatRequestEntry = null;
        els.frManufacturer.value = ""; els.frSampleUrl.value = "";
        els.frSampleAccessCode.value = "";
        els.frExt.value = ""; els.frMessage.value = "";
        els.frContact.value = ""; els.frSample.value = "";
      },
      onError: function (msg) { toast(msg, "error"); },
    }).then(function () {
      // 收口后才解锁（在途期保持禁用：重复点击不再各发一条请求）
      if (els.frSubmit) els.frSubmit.disabled = false;
    });
  }

  // ---- P2 复核 2026-10-20（问题 3）：上传队列跟随选择入口 ----
  // 行容器（#upload-progress-list）是**唯一**的任务列表：抽屉打开时整容器
  // 移动到抽屉宿主（#import-upload-host，与「选择文件」同一面板），关闭时移
  // 回侧栏。移动 DOM 节点保留其监听器，行不需要任何改造；不存在第二份独立
  // 任务列表。侧栏折叠/小屏时抽屉自持宿主，进度与取消（重试）始终可达。
  var uploadListHome = null;   // 行容器的原始父节点（侧栏）
  function syncUploadRowLocation() {
    var list = els.uploadProgressList;
    if (!list || !list.parentNode) return;
    if (!uploadListHome) uploadListHome = list.parentNode;
    var host = (importDrawerState.open && els.importUploadHost) || uploadListHome;
    if (list.parentNode !== host) host.appendChild(list);
  }

  function initUploadRowHostSync() {
    var list = els.uploadProgressList;
    if (!list) return;
    if (!uploadListHome) uploadListHome = list.parentNode;
    // 块显隐随行存在性切换（无行时整个「上传任务」块隐藏，不占空）；
    // MutationObserver 集中处理 makeUploadRow / removeLater / inlineRowRemove
    // 的所有增删路径，不必在每个调用点接线。
    var block = els.importUploadsBlock;
    if (block && typeof MutationObserver === "function") {
      var syncVisible = function () {
        block.hidden = list.childElementCount === 0;
      };
      new MutationObserver(syncVisible).observe(list, { childList: true });
      syncVisible();
    }
  }

  // ---------- 后台任务列表（GET /api/conversions?group=…；轮询） ----------
  function conversionStateLabel(state) {
    switch (state) {
      case "queued": return t("imp.task.state.queued");
      case "converting": case "validating": return t("imp.task.state.running");
      case "ready": return t("imp.task.state.ready");
      case "failed": return t("imp.task.state.failed");
      case "cancelled": return t("imp.task.state.cancelled");
      default: return state || "";
    }
  }

  /// 本机进行中的上传/转换任务摘要（P2 复核 2026-10-20 问题 3）：面板条目
  ///（inlineOrch）+ 直传/恢复行（pt.cos.jobs）。「后台任务」空态必须包含
  /// 它们——旧实现只查 /api/conversions，用户刚选完文件、浏览器正在本机
  /// 转换/上传时却显示「暂无进行中的任务」（复核脚本 picker-progress）。
  /// 这里只读同一线条目的**当前状态**（与行共用状态，不是第二份任务）。
  /// 本标签**正在进行**的直传任务（uploadFileCos 建引擎时登记、收口时清除；
  /// 刷新恢复的只读行不算——它们没有在传字节）。与面板条目共用去重键。
  var cosInflightJobs = {};

  function localUploadTaskSummaries(skipIngestionIds) {
    var out = [];
    var skip = skipIngestionIds || {};
    if (inlineOrch && typeof inlineOrch.list === "function") {
      inlineOrch.list().forEach(function (e) {
        if (!e) return;
        if (e.uploadJobId) skip[e.uploadJobId] = true;
        var active = e.state === "converting" || e.state === "convert_queued" ||
          e.state === "upload_queued" || e.state === "uploading" ||
          e.state === "requeue_convert" || e.state === "identity_wait" ||
          e.state === "need_disk_confirm";
        if (!active) return;
        var label = (e.stage && e.stage.key) ? tt(e.stage.key) : "";
        out.push({ name: e.artifactName || e.name, state: label });
      });
    }
    Object.keys(cosInflightJobs).forEach(function (id) {
      if (skip[id]) return;
      out.push({ name: cosInflightJobs[id],
                 state: tt("upload.cos.stage.uploading") });
    });
    return out;
  }

  /// 服务器转换任务缓存 + 本机队列的**立即**重画（不网罗网络请求）：面板条
  /// 目状态变化（ui.refresh）时只重算 localUploadTaskSummaries——选完文件的
  /// 一两秒内抽屉就要能看到任务，不等下一个 5s 轮询。
  function refreshImportTasksLocalOnly() {
    if (!importDrawerState.open || !els.importTaskList) return;
    renderImportTasks(importDrawerState.lastServerTasks || [],
                      localUploadTaskSummaries());
  }

  function refreshImportTasks() {
    if (!els.importTaskList || !importDrawerState.open) return;
    apiFetch("/api/conversions?group=open").then(jsonBody)
      .then(function (openRes) {
        if (openRes.status === 401 || openRes.status === 403) {
          importDrawerState.taskPollStopped = true;
          // 服务器任务拿不到时，本机队列照常展示（不能因此显示「无任务」）
          var local = localUploadTaskSummaries();
          importDrawerState.lastServerTasks = [];
          if (local.length) renderImportTasks([], local);
          else els.importTaskList.textContent = t("imp.tasks.auth.stop");
          return null;
        }
        // recent 合并展示（open 优先去重；失败不影响 open 列表）
        return apiFetch("/api/conversions?group=recent").then(jsonBody)
          .then(function (recentRes) { return { openRes: openRes, recentRes: recentRes }; });
      })
      .then(function (res) {
        if (!res) return;
        var seen = {};
        var items = [];
        ((res.openRes.body && res.openRes.body.items) || []).forEach(function (j) {
          if (!seen[j.conversion_job_id]) { seen[j.conversion_job_id] = true; items.push(j); }
        });
        ((res.recentRes.body && res.recentRes.body.items) || []).forEach(function (j) {
          if (!seen[j.conversion_job_id]) { seen[j.conversion_job_id] = true; items.push(j); }
        });
        importDrawerState.lastServerTasks = items;
        renderImportTasks(items, localUploadTaskSummaries());
      })
      .catch(function () {
        // 网络故障：明确「暂时无法获取进度」，下次轮询继续（不判失败）；
        // 本机队列仍在进行时照常列出（不显示「无任务」）。
        var local = localUploadTaskSummaries();
        if (els.importTaskList) {
          if (local.length) renderImportTasks([], local);
          else els.importTaskList.textContent = t("imp.tasks.net.err");
        }
      });
  }

  function renderImportTasks(items, localItems) {
    if (!els.importTaskList) return;
    els.importTaskList.innerHTML = "";
    var locals = localItems || [];
    if ((!items || !items.length) && !locals.length) {
      els.importTaskList.textContent = t("imp.tasks.empty");
      return;
    }
    // 本机队列（面板转换/COS 上传）在前：用户刚选的文件排最前（P2 问题 3）
    locals.forEach(function (lt) {
      var row = document.createElement("div");
      row.className = "imp-task-row state-local";
      var mid = document.createElement("div");
      mid.className = "imp-task-mid";
      var nm = document.createElement("div");
      nm.className = "imp-task-name";
      nm.textContent = truncateMiddle(String(lt.name || "?"), 36);
      var st = document.createElement("div");
      st.className = "imp-task-state";
      st.textContent = lt.state || "";
      mid.appendChild(nm);
      mid.appendChild(st);
      row.appendChild(mid);
      els.importTaskList.appendChild(row);
    });
    (items || []).slice(0, 20).forEach(function (job) {
      var row = document.createElement("div");
      row.className = "imp-task-row state-" + (job.state || "");
      var mid = document.createElement("div");
      mid.className = "imp-task-mid";
      var nm = document.createElement("div");
      nm.className = "imp-task-name";
      nm.textContent = truncateMiddle(job.source_name || job.canonical_name || job.conversion_job_id, 36);
      var st = document.createElement("div");
      st.className = "imp-task-state";
      st.textContent = conversionStateLabel(job.state) +
        (job.error_code ? " · " + job.error_code : "");
      mid.appendChild(nm);
      mid.appendChild(st);
      row.appendChild(mid);
      if (job.state === "ready") {
        var openBtn = document.createElement("button");
        openBtn.type = "button";
        openBtn.className = "btn secondary small";
        openBtn.textContent = t("imp.task.open");
        openBtn.addEventListener("click", function () {
          // 用户显式点击才打开（持久列表绝不自动抢占正在看的切片）。
          // P2：任务视图无 slide_id——按 canonical 名在列表解析 id 后打开；
          // 列表外/解析不到回落名（console.debug 由调用侧统一，P3 拆除）
          var n = job.canonical_name || job.source_name;
          if (!n) return;
          var s = findSlideInfo(n);
          var ref = (s && s.slide_id) || null;
          if (!ref) {
            console.debug("[slide-id] 转换任务打开无 slide_id，按名回落（旧后端兼容）:", n);
            ref = n;
          }
          openSlide(ref);
        });
        row.appendChild(openBtn);
      } else if (job.state === "failed" || job.state === "cancelled") {
        var retryBtn = document.createElement("button");
        retryBtn.type = "button";
        retryBtn.className = "btn secondary small";
        retryBtn.textContent = t("imp.task.retry");
        retryBtn.addEventListener("click", function () {
          retryBtn.disabled = true;
          apiFetch("/api/conversions/" + encodeURIComponent(job.conversion_job_id) + "/retry",
                   { method: "POST" })
            .then(jsonBody)
            .then(function (res) {
              if (!res.ok) {
                toast((res.body && res.body.error) || t("imp.task.retry.fail"), "error");
                retryBtn.disabled = false;
                return;
              }
              toast(t("imp.task.retry.queued"), "success");
              refreshImportTasks();
            })
            .catch(function () { toast(t("imp.task.retry.fail"), "error"); retryBtn.disabled = false; });
        });
        row.appendChild(retryBtn);
      }
      els.importTaskList.appendChild(row);
    });
  }

  function startImportTaskPolling() {
    stopImportTaskPolling();
    importDrawerState.taskPollStopped = false;
    importDrawerState.taskTimer = setInterval(function () {
      if (!importDrawerState.taskPollStopped) refreshImportTasks();
    }, 5000);
  }

  function stopImportTaskPolling() {
    if (importDrawerState.taskTimer) {
      clearInterval(importDrawerState.taskTimer);
      importDrawerState.taskTimer = null;
    }
  }

  // ---------- 百度分享页签 ----------
  var baiduState = {
    capsChecked: false,
    enumerationAvailable: false,
    importAvailable: false,
    workerEnabled: true,
    reasonCode: "",
    enumId: null,
    enumState: null,
    enumComplete: false,
    enumErrorCode: "",
    enumTimer: null,
    candidates: [],      // 当前页条目
    cursor: null,        // 当前页游标（null = 第一页）
    nextCursor: null,
    prevStack: [],
    selected: {},        // candidate id → candidate（跨页保留）
    importId: null,
    importTimer: null,
    importIdemKey: null,
  };

  function baiduCapReasonText(code) {
    switch (code) {
      case "enumeration_disabled": return t("bd.cap.reason.enumeration_disabled");
      case "connector_missing": return t("bd.cap.reason.connector_missing");
      case "secret_unconfigured": return t("bd.cap.reason.secret_unconfigured");
      case "import_disabled": return t("bd.cap.reason.import_disabled");
      default: return t("bd.cap.reason.unknown");
    }
  }

  function setBaiduListEnabled(enabled) {
    if (els.baiduListBtn) els.baiduListBtn.disabled = !enabled;
  }

  function setBaiduImportEnabled(enabled, reason) {
    if (!els.baiduImportBtn) return;
    els.baiduImportBtn.disabled = !enabled;
    els.baiduImportBtn.title = enabled ? "" : (reason || "");
  }

  function renderBaiduCapabilities() {
    if (!els.baiduCapStatus) return;
    if (!baiduState.enumerationAvailable) {
      // 页签仍可见：给出可行动原因（不呈现无响应按钮）
      els.baiduCapStatus.textContent = t("bd.cap.unavailable", {
        reason: baiduCapReasonText(baiduState.reasonCode),
      });
      els.baiduCapStatus.hidden = false;
      setBaiduListEnabled(false);
      setBaiduImportEnabled(false, baiduCapReasonText(baiduState.reasonCode));
      if (els.baiduInputBlock) els.baiduInputBlock.hidden = true;
      if (els.baiduCandidatesBlock) els.baiduCandidatesBlock.hidden = true;
      return;
    }
    els.baiduCapStatus.textContent = baiduState.importAvailable
      ? t("bd.cap.ready")
      : t("bd.cap.enum.only");
    // 可枚举但部署未拉起 worker：提交后会一直排队。只提示、不藏表单
    // （worker 可能在另一进程/容器；是否开启属部署配置问题）
    if (!baiduState.workerEnabled) {
      els.baiduCapStatus.textContent += " · " + t("bd.cap.worker.off");
    }
    els.baiduCapStatus.hidden = false;
    setBaiduListEnabled(true);
    if (els.baiduInputBlock) els.baiduInputBlock.hidden = false;
    // 可枚举但不可导入：允许读列表，禁用导入
    setBaiduImportEnabled(false, t("bd.cap.reason.import_disabled"));
  }

  function refreshBaiduCapabilities() {
    if (!els.baiduCapStatus) return Promise.resolve();
    return apiFetch("/api/remote-imports/baidu/capabilities").then(jsonBody).then(function (res) {
      baiduState.capsChecked = true;
      if (!res.ok || !res.body) {
        // 503 connector_unavailable 等：按不可用处理（带原因）
        baiduState.enumerationAvailable = false;
        baiduState.importAvailable = false;
        baiduState.reasonCode = (res.body && res.body.code) || "";
        renderBaiduCapabilities();
        return;
      }
      baiduState.enumerationAvailable = !!res.body.enumeration_available;
      baiduState.importAvailable = !!res.body.import_available;
      baiduState.workerEnabled = !!res.body.worker_enabled;
      baiduState.reasonCode = res.body.reason_code || "";
      renderBaiduCapabilities();
    }).catch(function () {
      baiduState.capsChecked = true;
      baiduState.enumerationAvailable = false;
      baiduState.importAvailable = false;
      baiduState.reasonCode = "";
      renderBaiduCapabilities();
    });
  }

  function setBaiduEnumStatus(msg) {
    if (!els.baiduEnumStatus) return;
    els.baiduEnumStatus.textContent = msg || "";
    els.baiduEnumStatus.hidden = !msg;
  }

  function startBaiduEnumeration() {
    if (!els.baiduShareText || !baiduState.enumerationAvailable) return;
    var shareText = (els.baiduShareText.value || "").trim();
    if (!shareText) { toast(t("bd.share.need"), "error"); return; }
    var code = (els.baiduExtractCode && els.baiduExtractCode.value || "").trim();
    var payload = { share_text: shareText };
    if (code) payload.extraction_code = code;   // 提取码只进请求体，不写日志
    setBaiduListEnabled(false);
    setBaiduEnumStatus(t("bd.enum.starting"));
    apiFetch("/api/remote-imports/baidu/enumerations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(jsonBody).then(function (res) {
      if (res.status !== 202 || !res.body || !res.body.id) {
        setBaiduListEnabled(true);
        setBaiduEnumStatus(t("bd.enum.create.fail", {
          e: (res.body && (res.body.error || res.body.code)) || ("HTTP " + res.status),
        }));
        return;
      }
      baiduState.enumId = res.body.id;
      baiduState.enumState = res.body.state || "queued";
      baiduState.selected = {};
      pollBaiduEnumeration();
    }).catch(function () {
      setBaiduListEnabled(true);
      setBaiduEnumStatus(t("bd.enum.create.fail", { e: t("imp.tasks.net.err") }));
    });
  }

  function pollBaiduEnumeration() {
    if (!baiduState.enumId) return;
    if (baiduState.enumTimer) clearTimeout(baiduState.enumTimer);
    var tick = function () {
      apiFetch("/api/remote-imports/baidu/enumerations/" +
               encodeURIComponent(baiduState.enumId)).then(jsonBody).then(function (res) {
        if (!res.ok || !res.body) {
          setBaiduEnumStatus(t("bd.enum.poll.fail", {
            e: (res.body && res.body.error) || ("HTTP " + res.status),
          }));
          baiduState.enumTimer = setTimeout(tick, 5000);
          return;
        }
        var b = res.body;
        baiduState.enumState = b.state;
        if (b.state === "queued" || b.state === "enumerating") {
          // 排队 ≠ 已扫出 0 项：queued 用独立文案，enumerating 才报进度
          setBaiduEnumStatus(t(
            b.state === "queued" ? "bd.enum.queued" : "bd.enum.progress",
            { n: b.scanned_count || 0 }));
          baiduState.enumTimer = setTimeout(tick, 2000);
          return;
        }
        setBaiduListEnabled(true);
        if (b.state === "failed" || b.state === "expired") {
          baiduState.enumComplete = false;
          baiduState.enumErrorCode = b.error_code || b.state;
          setBaiduEnumStatus(t("bd.enum.failed", {
            code: baiduState.enumErrorCode,
          }));
          if (els.baiduCandidatesBlock) els.baiduCandidatesBlock.hidden = true;
          return;
        }
        // ready：complete 才可导入；不完整禁用导入并说明
        baiduState.enumComplete = !!b.complete;
        setBaiduEnumStatus(t("bd.enum.ready", {
          n: b.candidate_count != null ? b.candidate_count : (b.scanned_count || 0),
        }) + (b.complete ? "" : " · " + t("bd.enum.incomplete", {
          reason: b.incomplete_reason || b.error_code || "",
        })));
        if (els.baiduCandidatesBlock) els.baiduCandidatesBlock.hidden = false;
        baiduState.cursor = null;
        baiduState.nextCursor = null;
        baiduState.prevStack = [];
        loadBaiduCandidates();
        updateBaiduImportGate();
      }).catch(function () {
        setBaiduEnumStatus(t("bd.enum.poll.fail", { e: t("imp.tasks.net.err") }));
        baiduState.enumTimer = setTimeout(tick, 5000);
      });
    };
    tick();
  }

  // 大小展示：十进制字符串 → 可读单位（BigInt 全程，不经 Number）
  function formatDecBytes(dec) {
    try {
      var b = BigInt(dec == null ? 0 : dec);
      if (b < 1024n) return b.toString() + " B";
      var units = ["KB", "MB", "GB", "TB"];
      var u = -1n;
      var v = b;
      do {
        v /= 1024n;
        u += 1n;
      } while (v >= 1024n && u < BigInt(units.length - 1));
      // 一位小数（整数部分 + 千分位余数；不再回落 Number）
      var tenths = ((b * 10n) / (1024n ** (u + 1n))) % 10n;
      var whole = v.toString();
      return whole + "." + tenths.toString() + " " + units[Number(u)];
    } catch (e) {
      return String(dec == null ? "" : dec);
    }
  }

  function loadBaiduCandidates() {
    if (!baiduState.enumId || !els.baiduCandidateList) return Promise.resolve();
    var url = "/api/remote-imports/baidu/enumerations/" +
      encodeURIComponent(baiduState.enumId) + "/candidates?limit=50";
    if (baiduState.cursor) url += "&cursor=" + encodeURIComponent(baiduState.cursor);
    return apiFetch(url).then(jsonBody).then(function (res) {
      if (!res.ok || !res.body || !Array.isArray(res.body.items)) {
        toast((res.body && res.body.error) || t("bd.candidates.fail"), "error");
        return;
      }
      baiduState.candidates = res.body.items;
      baiduState.nextCursor = res.body.next_cursor || null;
      renderBaiduCandidates();
    }).catch(function () { toast(t("bd.candidates.fail"), "error"); });
  }

  function baiduCandidateReasonText(code) {
    if (!code) return "";
    if (code === "directory") return t("bd.cand.reason.directory");
    if (code === "bundle_incomplete") return t("bd.cand.reason.bundle_incomplete");
    if (code === "unsupported") return t("bd.cand.reason.unsupported");
    // 阶段 1：非 TIFF 类候选（请先在本机转换为 OME-TIFF 后再上传）
    if (code === "convert_in_browser_first") {
      return t("bd.cand.reason.convert_in_browser_first");
    }
    return code;
  }

  function renderBaiduCandidates() {
    if (!els.baiduCandidateList) return;
    var filter = (els.baiduSearch && els.baiduSearch.value || "").trim().toLowerCase();
    var items = baiduState.candidates.filter(function (c) {
      if (!filter) return true;
      return ((c.name || "") + " " + (c.relative_path || "")).toLowerCase().indexOf(filter) >= 0;
    });
    els.baiduCandidateList.innerHTML = "";
    if (!items.length) {
      var empty = document.createElement("div");
      empty.className = "imp-hint";
      empty.textContent = t("bd.candidates.empty");
      els.baiduCandidateList.appendChild(empty);
    }
    items.forEach(function (c) {
      var row = document.createElement("label");
      row.className = "baidu-cand-row" + (c.selectable ? "" : " not-selectable");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = c.id;
      cb.checked = !!baiduState.selected[c.id];
      cb.disabled = !c.selectable;   // 目录/缺包/不支持：不可选
      cb.addEventListener("change", function () {
        toggleBaiduCandidate(c, cb.checked);
      });
      row.appendChild(cb);
      var info = document.createElement("span");
      info.className = "bc-info";
      var nm = document.createElement("span");
      nm.className = "bc-name";
      nm.textContent = c.name || c.id;
      nm.title = c.relative_path || c.name || "";
      var meta = document.createElement("span");
      meta.className = "bc-meta";
      meta.textContent = (c.relative_path || "") + " · " + formatDecBytes(c.size_bytes) +
        (c.format ? " · " + c.format : "") +
        (c.selectable ? "" : " · " + baiduCandidateReasonText(c.reason_code));
      info.appendChild(nm);
      info.appendChild(meta);
      row.appendChild(info);
      els.baiduCandidateList.appendChild(row);
    });
    if (els.baiduPrevBtn) els.baiduPrevBtn.disabled = baiduState.prevStack.length === 0;
    if (els.baiduNextBtn) els.baiduNextBtn.disabled = !baiduState.nextCursor;
    if (els.baiduPageInfo) {
      els.baiduPageInfo.textContent = t("bd.page.info", {
        n: baiduState.candidates.length,
        sel: Object.keys(baiduState.selected).length,
      });
    }
    updateBaiduSelectionSummary();
  }

  // 选择跨页保留（Map：candidate id → candidate）
  function toggleBaiduCandidate(c, on) {
    if (!c || !c.selectable) return;
    if (on) baiduState.selected[c.id] = c;
    else delete baiduState.selected[c.id];
    updateBaiduSelectionSummary();
    if (els.baiduPageInfo) {
      els.baiduPageInfo.textContent = t("bd.page.info", {
        n: baiduState.candidates.length,
        sel: Object.keys(baiduState.selected).length,
      });
    }
  }

  function baiduSelectedTotalBytes() {
    var total = 0n;
    Object.keys(baiduState.selected).forEach(function (k) {
      try { total += BigInt(baiduState.selected[k].size_bytes || 0); }
      catch (e) { /* 畸形条目跳过 */ }
    });
    return total;   // BigInt（十进制字符串来源，不经 Number）
  }

  function updateBaiduSelectionSummary() {
    if (!els.baiduSelectionSummary) return;
    var n = Object.keys(baiduState.selected).length;
    if (!n) {
      els.baiduSelectionSummary.textContent = t("bd.selected.none");
      return;
    }
    els.baiduSelectionSummary.textContent = t("bd.selected.summary", {
      n: n,
      size: formatDecBytes(baiduSelectedTotalBytes().toString()),
    });
  }

  // 导入门：枚举 ready+complete、能力允许、有选择
  function updateBaiduImportGate() {
    var ok = baiduState.importAvailable && baiduState.enumComplete &&
      Object.keys(baiduState.selected).length > 0;
    setBaiduImportEnabled(ok, !baiduState.importAvailable
      ? t("bd.cap.reason.import_disabled")
      : (!baiduState.enumComplete ? t("bd.enum.incomplete.short") : ""));
  }

  function startBaiduImport() {
    if (!baiduState.importAvailable || !baiduState.enumId || !baiduState.enumComplete) return;
    var ids = Object.keys(baiduState.selected);
    if (!ids.length) { toast(t("bd.selected.none"), "error"); return; }
    var payload = { enumeration_id: baiduState.enumId, candidate_ids: ids };
    var targetPid = (els.baiduTargetSelect && els.baiduTargetSelect.value) || "";
    if (targetPid) payload.target_project_id = targetPid;
    // Idempotency-Key：每次确认点击生成；网络失败重试复用（成功后清除）
    if (!baiduState.importIdemKey) baiduState.importIdemKey = uuid();
    var key = baiduState.importIdemKey;
    if (els.baiduImportBtn) els.baiduImportBtn.disabled = true;
    setBaiduImportStatus(t("bd.import.submitting"));
    apiFetch("/api/remote-imports/baidu/imports", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify(payload),
    }).then(jsonBody).then(function (res) {
      if (res.status !== 202 || !res.body || !res.body.id) {
        baiduState.importIdemKey = key;   // 保留：同一确认重试复用
        if (els.baiduImportBtn) els.baiduImportBtn.disabled = false;
        setBaiduImportStatus(t("bd.import.fail", {
          e: (res.body && (res.body.error || res.body.code)) || ("HTTP " + res.status),
        }));
        return;
      }
      baiduState.importIdemKey = null;    // 成功：下次确认重新生成
      baiduState.importId = res.body.id;
      setBaiduImportStatus(t("bd.import.started", { id: res.body.id }));
      pollBaiduImport();
    }).catch(function () {
      baiduState.importIdemKey = key;     // 网络失败：重试复用同一键
      if (els.baiduImportBtn) els.baiduImportBtn.disabled = false;
      setBaiduImportStatus(t("bd.import.fail", { e: t("imp.tasks.net.err") }));
    });
  }

  function setBaiduImportStatus(msg) {
    if (!els.baiduImportStatus) return;
    els.baiduImportStatus.textContent = msg || "";
    els.baiduImportStatus.hidden = !msg;
  }

  function bdStageLabel(stage) {
    switch (stage) {
      case "queued": return t("bd.stage.queued");
      case "transferring": return t("bd.stage.transferring");
      case "downloading": return t("bd.stage.downloading");
      case "validating": return t("bd.stage.validating");
      case "converting": return t("bd.stage.converting");
      case "ingesting": return t("bd.stage.ingesting");
      case "ready": return t("bd.stage.ready");
      case "failed": return t("bd.stage.failed");
      case "cancelled": return t("bd.stage.cancelled");
      default: return stage || "";
    }
  }

  function renderBaiduImport(batch) {
    if (!els.baiduImportItems) return;
    els.baiduImportItems.innerHTML = "";
    var counts = { ready: 0, failed: 0, other: 0 };
    (batch.items || []).forEach(function (it) {
      if (it.stage === "ready") counts.ready++;
      else if (it.stage === "failed" || it.stage === "cancelled") counts.failed++;
      else counts.other++;
      var row = document.createElement("div");
      row.className = "baidu-import-row stage-" + (it.stage || "");
      var nm = document.createElement("span");
      nm.className = "bi-name";
      nm.textContent = truncateMiddle(it.name || it.id, 30);
      var stg = document.createElement("span");
      stg.className = "bi-stage";
      stg.textContent = bdStageLabel(it.stage) +
        (it.error_code ? " · " + it.error_code : "");
      row.appendChild(nm);
      row.appendChild(stg);
      els.baiduImportItems.appendChild(row);
    });
    setBaiduImportStatus(t("bd.import.progress", {
      state: batch.state || "",
      ok: counts.ready,
      fail: counts.failed,
      total: (batch.items || []).length,
    }));
    // 部分失败（批次终态）：显示统计 + 仅重试失败项
    if (batch.state === "partial_failed" || batch.state === "failed") {
      var failedIds = (batch.items || [])
        .filter(function (it) { return it.stage === "failed"; })
        .map(function (it) { return it.id; });
      if (failedIds.length) {
        var retryBtn = document.createElement("button");
        retryBtn.type = "button";
        retryBtn.className = "btn secondary small";
        retryBtn.textContent = t("bd.import.retry.failed", { n: failedIds.length });
        if (!baiduState.importAvailable) {
          retryBtn.disabled = true;
          retryBtn.title = baiduCapReasonText(baiduState.reasonCode || "import_disabled");
        }
        retryBtn.addEventListener("click", function () {
          if (!baiduState.importAvailable) return;
          retryBtn.disabled = true;
          apiFetch("/api/remote-imports/baidu/imports/" +
                   encodeURIComponent(batch.id) + "/retry", {
            method: "POST",
            headers: { "Content-Type": "application/json", "Idempotency-Key": uuid() },
            body: JSON.stringify({ item_ids: failedIds }),
          }).then(jsonBody).then(function (res) {
            if (!res.ok) {
              toast((res.body && res.body.error) || t("bd.import.fail.short"), "error");
              retryBtn.disabled = false;
              return;
            }
            toast(t("imp.task.retry.queued"), "success");
            pollBaiduImport();
          }).catch(function () {
            toast(t("bd.import.fail.short"), "error");
            retryBtn.disabled = false;
          });
        });
        els.baiduImportItems.appendChild(retryBtn);
      }
    }
  }

  function pollBaiduImport() {
    if (!baiduState.importId) return;
    if (baiduState.importTimer) clearTimeout(baiduState.importTimer);
    var tick = function () {
      apiFetch("/api/remote-imports/baidu/imports/" +
               encodeURIComponent(baiduState.importId)).then(jsonBody).then(function (res) {
        if (!res.ok || !res.body) {
          setBaiduImportStatus(t("bd.import.fail", {
            e: (res.body && res.body.error) || ("HTTP " + res.status),
          }));
          baiduState.importTimer = setTimeout(tick, 5000);
          return;
        }
        renderBaiduImport(res.body);
        var st = res.body.state;
        if (st === "succeeded" || st === "partial_failed" || st === "failed" ||
            st === "cancelled") {
          baiduState.importTimer = null;   // 终态：停止轮询
          reloadProjectsAndUnfiled();
          updateBaiduImportGate();
          return;
        }
        baiduState.importTimer = setTimeout(tick, 2500);
      }).catch(function () {
        setBaiduImportStatus(t("bd.import.fail", { e: t("imp.tasks.net.err") }));
        baiduState.importTimer = setTimeout(tick, 5000);
      });
    };
    tick();
  }

  // ---------- 抽屉开合 / 页签 ----------
  function switchImportTab(which) {
    importDrawerState.tab = which === "baidu" ? "baidu" : "local";
    var isLocal = importDrawerState.tab === "local";
    if (els.importTabLocal) {
      els.importTabLocal.classList.toggle("active", isLocal);
      els.importTabLocal.setAttribute("aria-selected", isLocal ? "true" : "false");
    }
    if (els.importTabBaidu) {
      els.importTabBaidu.classList.toggle("active", !isLocal);
      els.importTabBaidu.setAttribute("aria-selected", !isLocal ? "true" : "false");
    }
    if (els.importPanelLocal) els.importPanelLocal.hidden = !isLocal;
    if (els.importPanelBaidu) els.importPanelBaidu.hidden = isLocal;
    if (!isLocal && !baiduState.capsChecked) refreshBaiduCapabilities();
  }

  function openImportDrawer(triggerEl) {
    if (!els.importDrawer) return;
    triggerEl = closeSidebarDrawerUnderOverlay(triggerEl);
    importDrawerState.open = true;
    importDrawerState.lastFocusEl = triggerEl || null;
    // folder-experience §1：一次打开 = 一次新的导入选择会话——generation 前进、
    // dirty 复位；默认目标等**最新授权项目列表**返回后再落（且该次会话未被
    // 重开、用户未先操作），迟到的响应不得覆盖用户已做的选择。
    importDrawerState.targetGen += 1;
    var targetGen = importDrawerState.targetGen;
    importDrawerState.targetDirty = false;
    els.importDrawer.hidden = false;
    if (els.importDrawerMask) els.importDrawerMask.hidden = false;
    // P2 问题 3：行容器移动到抽屉宿主（与选择文件的同一面板；关抽屉移回）
    syncUploadRowLocation();
    // 目标下拉按当前项目列表重建（会话内选择保留）
    var listFresh = false;
    apiFetch("/api/projects").then(jsonBody).then(function (res) {
      if (res.ok && Array.isArray(res.body)) {
        allProjects = res.body;
        listFresh = true;   // 只有拿到最新授权列表才允许落默认值
      }
    }).catch(function () { /* 保留当前缓存 */ }).then(function () {
      if (targetGen !== importDrawerState.targetGen) return;   // 抽屉已重开
      renderImportTargetSelects();
      if (listFresh) applyImportTargetDefault();
    });
    loadImportFormatCatalog();
    renderFrSampleMax();
    loadRecentFormatRequests();
    refreshImportTasks();
    startImportTaskPolling();
    switchImportTab(importDrawerState.tab);
    try {
      var first = els.importDrawerClose || els.importDrawer;
      if (first && typeof first.focus === "function") first.focus();
    } catch (e) { /* 忽略聚焦失败 */ }
  }

  function closeImportDrawer() {
    if (!els.importDrawer) return;
    importDrawerState.open = false;
    els.importDrawer.hidden = true;
    if (els.importDrawerMask) els.importDrawerMask.hidden = true;
    // P2 问题 3：行容器移回侧栏（唯一列表，不复制）
    syncUploadRowLocation();
    stopImportTaskPolling();
    var back = importDrawerState.lastFocusEl;
    importDrawerState.lastFocusEl = null;
    if (back && typeof back.focus === "function") {
      try { back.focus(); } catch (e) { /* 触发按钮可能已移除 */ }
    }
  }

  // 抽屉焦点圈闭：Tab 在抽屉可聚焦元素间循环（不落回背景）
  function trapDrawerFocus(e) {
    var drawer = els.importDrawer;
    if (!drawer) return;
    var focusables = [];
    try {
      focusables = Array.prototype.slice.call(drawer.querySelectorAll(
        "button:not([disabled]), input:not([disabled]), select, textarea"))
        .filter(function (el) { return !el.hidden; });
    } catch (err) { return; }
    if (!focusables.length) return;
    var active = document.activeElement;
    var first = focusables[0], last = focusables[focusables.length - 1];
    var inside = focusables.indexOf(active) >= 0;
    if (e.shiftKey && (!inside || active === first)) {
      e.preventDefault();
      try { last.focus(); } catch (err) {}
    } else if (!e.shiftKey && (!inside || active === last)) {
      e.preventDefault();
      try { first.focus(); } catch (err) {}
    }
  }

  // ---------- 事件绑定 ----------
  // =========================================================================
  // 顶栏信息架构（升级 Review 2026-09-09 §3.3–3.5）：
  //   - 工具栏 popover 通用定位/开关机制（矩形设置、标注选项、账户）
  //   - 账户 chip + popover + GET /api/account/balance 自助余额
  //   - 宽度断点分组（>=1440 全展开；1024–1439 折视图/标注组；<1024 只留
  //     当前工具、AI、倍率、账户，其余入 ⋯）
  // =========================================================================

  // ---------- nano-CNY 精确换算（§4.2；与 plugins/pathtogether-admin
  // ui/main.js formatCny2 同一算法：十进制字符串/BigInt → 两位小数，半分
  // 进位、绝对值方向舍入，全程不经 Number/toFixed，>2^53 不失真） ----------
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
  function acctCny(v) {
    var s = formatCny2(v);
    return s === null ? null : s + " CNY";
  }

  // ---------- Credit 格式化（0088；units / 10^7，截断 4 位小数，BigInt） ----------
  // 非零但 < 0.0001 显示「小于 0.0001」（不能显示为 0/免费）。
  function acctGroupThousands(digits) {
    var out = "", count = 0;
    for (var i = digits.length - 1; i >= 0; i--) {
      out = digits.charAt(i) + out;
      count += 1;
      if (count % 3 === 0 && i > 0) out = "," + out;
    }
    return out;
  }

  function formatCredits4(v) {
    if (v === null || v === undefined || v === "") return null;
    var b;
    try { b = BigInt(v); } catch (e) { return null; }
    var neg = b < 0n;
    if (neg) b = -b;
    var whole = b / 10000000n;
    var frac = (b % 10000000n) / 1000n;
    if (whole === 0n && frac === 0n) {
      return b !== 0n ? t("acct.credits.less_than_min") : "0";
    }
    var fracText = frac.toString().padStart(4, "0").replace(/0+$/, "");
    var text = acctGroupThousands(whole.toString()) +
      (fracText ? "." + fracText : "");
    return (neg ? "-" : "") + text;
  }

  function acctCreditsUnit(v) {
    var s = formatCredits4(v);
    return s === null ? null : s + " Credits";
  }

  // 可用余额取整显示（2026-11-05 产品决定）：向下取整到整数 Credit，
  // 绝不显示多于用户实际持有的数。非零但不足 1 显示「不足 1」；0 与负值
  // 均显示 0。BigInt 全程不经 Number，大数值不失真。仅用于可用余额大数字；
  // 预留/退款冻结/明细仍用 4 位小数。
  function formatCreditsWhole(v) {
    if (v === null || v === undefined || v === "") return null;
    var b;
    try { b = BigInt(v); } catch (e) { return null; }
    if (b <= 0n) return "0";
    if (b < 10000000n) return t("acct.credits.less_than_one");
    return acctGroupThousands((b / 10000000n).toString());
  }
  // 用量条副行取整：BigInt 整数 Credit（非法/负值按 0）；调用方保证非负数。
  function acctWholeCredits(v) {
    var b = acctBig(v);
    if (b === null || b <= 0n) return 0n;
    return b / 10000000n;
  }

  // 规范化邮箱 @ 前的 local-part；非邮箱存量账号回退完整 login_id
  function acctDisplayUsername(username) {
    var s = String(username == null ? "" : username);
    if (!s) return "";
    var at = s.indexOf("@");
    return at > 0 ? s.slice(0, at) : s;
  }

  // ---------- 工具栏 popover 机制 ----------
  // #toolbar overflow-x:auto 会裁剪内部绝对定位后代，因此浮层用 fixed 定位，
  // 打开时按触发按钮 getBoundingClientRect 计算；窗口 resize / 工具栏滚动 /
  // Escape / 点击外部即关闭。同一机制服务矩形设置、标注选项与账户三个浮层。
  var toolbarPopClosers = [];   // 已注册浮层的关闭函数（打开 ⋯ 菜单时统一收起）
  var closeTbbMoreMenu = function () {}; // bindTbbMore 里被真实 closeMore 覆盖

  // 打开期间把浮层挂到 body：#toolbar 的 overflow 裁剪与移动端底栏的
  // backdrop-filter（fixed 包含块）都不再影响定位。
  function ensurePopInBody(pop) {
    try {
      if (pop && pop.parentNode !== document.body && document.body && document.body.appendChild) {
        document.body.appendChild(pop);
      }
    } catch (e) { /* 保持原位 */ }
  }

  function positionToolbarPop(btn, pop) {
    if (!btn || !pop) return;
    ensurePopInBody(pop);
    var r = btn.getBoundingClientRect();
    if (!r) return;
    var pw = pop.offsetWidth || 0;
    var ph = pop.offsetHeight || 0;
    var vw = window.innerWidth || 1024;
    var vh = window.innerHeight || 768;
    var left = r.right - pw;
    if (left < 8) left = 8;
    if (left + pw > vw - 8) left = vw - 8 - pw;
    if (left < 8) left = 8;
    var top = r.bottom + 6;
    // 下方空间不足（如移动端底栏触发）：向上弹
    if (top + ph > vh - 8 && r.top - ph - 6 >= 8) {
      top = r.top - ph - 6;
    }
    pop.style.left = Math.round(left) + "px";
    pop.style.top = Math.round(top) + "px";
  }

  function bindToolbarPop(btn, pop, opts) {
    opts = opts || {};
    if (!btn || !pop) return null;
    var open = false;
    // 程序化 open（分享选择器确认后重开浮层等）可能发生在同一 click 事件的
    // 冒泡路径上：紧随其后的 document click 不能按「点击外部」把刚打开的
    // 浮层关掉。一次性标记由下一个 document click 消费；setTimeout(0) 兜底
    // 重置——微任务检查点会出现在同一派发的两个 listener 之间（提前清标记），
    // 宏任务则保证等整个 click 派发结束后才跑，不吞之后真正的外点关闭。
    var skipNextOutsideClick = false;
    function setOpen(v) {
      v = !!v;
      if (v === open) { if (v) positionToolbarPop(btn, pop); return; }
      open = v;
      pop.hidden = !open;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        positionToolbarPop(btn, pop);
        // 打开一个浮层时收起其它浮层与 ⋯ 菜单（同一时间至多一个）
        toolbarPopClosers.forEach(function (fn) {
          if (fn !== closer) fn();
        });
        closeTbbMoreMenu();
        if (typeof opts.onOpen === "function") opts.onOpen();
      } else if (typeof opts.onClose === "function") {
        opts.onClose();
      }
    }
    function closer() { setOpen(false); }
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      setOpen(!open);
    });
    document.addEventListener("click", function (e) {
      if (!open) return;
      if (skipNextOutsideClick) { skipNextOutsideClick = false; return; }
      var tgt = e.target;
      if (tgt && tgt.closest &&
          (tgt.closest("#" + pop.id) || tgt.closest("#" + btn.id))) return;
      setOpen(false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && open) setOpen(false);
    });
    window.addEventListener("resize", function () { if (open) setOpen(false); });
    if (els.tbbMoreHost()) {
      els.tbbMoreHost().addEventListener("scroll", function () {
        if (open) setOpen(false);
      });
    }
    toolbarPopClosers.push(closer);
    return {
      open: function () {
        skipNextOutsideClick = true;
        setTimeout(function () { skipNextOutsideClick = false; }, 0);
        setOpen(true);
      },
      close: closer,
      isOpen: function () { return open; },
    };
  }

  // ---------- 账户 popover：余额拉取与渲染（§3.5） ----------
  // user=一次性总额度口径（total_allowance）；owner=当月窗口口径
  // （owner_month_window）。额度缺失（400 spend_total_allowance_missing）/
  // DB 不可用（503）/网络失败一律显示「额度信息暂不可用（原因）」，
  // 绝不显示 ¥0。额度数值是十进制字符串（nano-CNY），
  // 因 1 nano-CNY == 1 Credit unit，直接走 formatCredits4 的 BigInt 口径。

  function acctBig(v) {
    if (v === null || v === undefined || v === "") return null;
    try { return BigInt(v); } catch (e) { return null; }
  }

  // 用量条：used/total 传 BigInt（或整数字符串）。fill 宽度按十分位百分比，
  // 用量 > 0 时至少 2% 保证可见；≥80% 琥珀、≥95% danger。
  function acctMeterPaint(bar, fill, used, total) {
    if (!bar || !fill) return false;
    var u = acctBig(used);
    var tot = acctBig(total);
    if (u === null || tot === null || tot <= 0n) {
      acctMeterHide(bar, fill);
      return false;
    }
    if (u < 0n) u = 0n;
    var tenths = Number(u * 1000n / tot) / 10;
    if (tenths > 100) tenths = 100;
    var pct = Math.floor(tenths);
    if (u > 0n && tenths < 2) tenths = 2;
    if (fill.style) fill.style.width = tenths + "%";
    bar.classList.toggle("is-warn", tenths >= 80 && tenths < 95);
    bar.classList.toggle("is-danger", tenths >= 95);
    if (bar.setAttribute) bar.setAttribute("aria-valuenow", String(pct));
    bar.hidden = false;
    return true;
  }

  function acctMeterHide(bar, fill) {
    if (fill && fill.style) fill.style.width = "0%";
    if (!bar) return;
    bar.classList.remove("is-warn", "is-danger");
    if (bar.setAttribute) bar.setAttribute("aria-valuenow", "0");
    bar.hidden = true;
  }

  function acctUsageMeterHide() {
    acctMeterHide(els.acctPopUsage, els.acctPopUsageFill);
  }

  // 大数字只放数值，单位「Credits」是模板里的独立小字（无数值时隐藏）
  function acctSetAmount(text) {
    if (els.acctPopRemaining) els.acctPopRemaining.textContent = text === null ? "—" : text;
    if (els.acctPopUnit) els.acctPopUnit.hidden = text === null;
  }

  function acctBalanceUnavailable(status, code) {
    var key;
    if (status === 400 && code === "spend_total_allowance_missing") {
      key = "acct.balance.reason.missing";
    } else if (status === 503) {
      key = "acct.balance.reason.db";
    } else if (status === 401 || status === 403) {
      key = "acct.balance.reason.auth";
    } else if (status) {
      key = "acct.balance.reason.http";
    } else {
      key = "acct.balance.reason.network";
    }
    var reason = t(key, key === "acct.balance.reason.http" ? { status: String(status) } : null);
    if (els.acctPopScope) els.acctPopScope.textContent = "";
    acctSetAmount(null);
    acctUsageMeterHide();
    if (els.acctPopDetail) {
      els.acctPopDetail.textContent = t("acct.balance.unavailable", { reason: reason });
    }
    if (els.acctPopExtra) {
      els.acctPopExtra.hidden = true;
      els.acctPopExtra.textContent = "";
    }
    if (els.acctTopupLink) els.acctTopupLink.hidden = true;
  }

  // Credit 钱包（0088）：可用 Credit（4 位截断）/ 预占 / 退款冻结 / 受限说明；
  // 充值入口新标签打开 /app/credits（预览态只读，不显示入口）。
  function acctCreditsRestrictedText(reason) {
    var raw = String(reason || "");
    if (raw.indexOf("account_status_") === 0) {
      return t("credit.restricted.account_status");
    }
    if (raw.indexOf("frozen:") === 0) {
      return t("credit.restricted.frozen", { reason: raw.slice(7) });
    }
    if (raw === "pending_reconciliation") {
      return t("credit.restricted.pending_reconciliation");
    }
    return t("credit.restricted.other", { reason: raw });
  }

  function acctCreditsRender(data) {
    var subject = data.subject || {};
    var wallet = data.wallet || {};
    if (els.acctPopScope) {
      els.acctPopScope.textContent = t("acct.credits.scope");
    }
    acctSetAmount(formatCreditsWhole(wallet.available_units));
    // 大数字 = 可用（整数，向下取整）；进度分母 = 累计发放（issued_units，
    // 缺失/0 时隐藏条与副行）。副行同样取整：used = issued_int - available_int，
    // 保证屏上两个数字相加等于 issued_int。进度条仍用精确 BigInt。
    var available = acctBig(wallet.available_units);
    var issued = acctBig(wallet.issued_units);
    var used = null;
    if (available !== null && issued !== null && issued > 0n) {
      used = issued > available ? issued - available : 0n;
    }
    if (els.acctPopDetail) els.acctPopDetail.textContent = "";
    if (used !== null && acctMeterPaint(
        els.acctPopUsage, els.acctPopUsageFill, used, issued)) {
      if (els.acctPopDetail) {
        var issuedInt = acctWholeCredits(wallet.issued_units);
        var availableInt = acctWholeCredits(wallet.available_units);
        var usedInt = issuedInt > availableInt ? issuedInt - availableInt : 0n;
        els.acctPopDetail.textContent = t("acct.credits.used", {
          used: acctGroupThousands(usedInt.toString()),
          issued: acctGroupThousands(issuedInt.toString()),
        });
      }
    } else {
      acctUsageMeterHide();
    }
    if (els.acctPopExtra) {
      var rows = [];
      // 预占/退款冻结只在非零时显示（余额卡片保持简洁）
      var reserved = acctCreditsUnit(wallet.reserved_units);
      if (reserved !== null && String(wallet.reserved_units) !== "0") {
        rows.push(t("acct.credits.reserved") + " " + reserved);
      }
      var refundPending = acctCreditsUnit(wallet.refund_pending_units);
      if (refundPending !== null &&
          String(wallet.refund_pending_units) !== "0") {
        rows.push(t("acct.credits.refund_pending") + " " + refundPending);
      }
      if (wallet.restricted_reason) {
        rows.push(t("acct.credits.restricted", {
          reason: acctCreditsRestrictedText(wallet.restricted_reason),
        }));
      }
      els.acctPopExtra.textContent = rows.join(" · ");
      els.acctPopExtra.hidden = rows.length === 0;
    }
    if (els.acctTopupLink) {
      els.acctTopupLink.hidden = !!subject.preview;
      // can_topup 为假时入口仍指向 /app/credits（账本页），文案改为「Credit 明细」
      var linkKey = data.can_topup ? "acct.credits.topup" : "acct.credits.history";
      if (els.acctTopupLink.setAttribute) {
        els.acctTopupLink.setAttribute("data-i18n", linkKey);
      }
      els.acctTopupLink.textContent = t(linkKey);
    }
  }

  function acctBalanceRender(data) {
    data = data || {};
    if (data.billing_backend === "credits") {
      acctCreditsRender(data);
      return;
    }
    var isMonth = data.spend_target === "owner_month_window";
    if (els.acctTopupLink) els.acctTopupLink.hidden = true;
    if (els.acctPopScope) {
      var scope = t(isMonth ? "acct.balance.scope.month" : "acct.balance.scope.total");
      if (isMonth && data.period_end) {
        scope += " · " + t("acct.balance.reset", {
          date: String(data.period_end).slice(5, 10),
        });
      }
      els.acctPopScope.textContent = scope;
    }
    acctSetAmount(formatCreditsWhole(data.remaining_nano_cny));
    // legacy / owner 同口径：副行取整，used = 限额_int - 剩余_int（最小值 0；
    // 屏上两数相加等于限额_int）；进度条仍用精确值 spent + reserved。
    var limit = acctBig(data.limit_nano_cny);
    var spent = acctBig(data.spent_nano_cny);
    var remaining = acctBig(data.remaining_nano_cny);
    var reserved = acctBig(data.reserved_nano_cny) || 0n;
    if (els.acctPopDetail) els.acctPopDetail.textContent = "";
    if (limit !== null && spent !== null && remaining !== null && limit > 0n &&
        acctMeterPaint(els.acctPopUsage, els.acctPopUsageFill,
                       spent + reserved, limit)) {
      if (els.acctPopDetail) {
        var limitInt = acctWholeCredits(data.limit_nano_cny);
        var remainingInt = acctWholeCredits(data.remaining_nano_cny);
        var usedInt = limitInt > remainingInt ? limitInt - remainingInt : 0n;
        els.acctPopDetail.textContent = t("acct.balance.used", {
          used: acctGroupThousands(usedInt.toString()),
          total: acctGroupThousands(limitInt.toString()),
        });
      }
    } else {
      acctUsageMeterHide();
    }
    if (els.acctPopExtra) {
      els.acctPopExtra.textContent = "";
      els.acctPopExtra.hidden = true;
    }
  }

  // 存储用量（2026-11-04）：与余额同一 popover 打开时拉取；只读自助接口
  // （/api/account/storage，不接受 user_id）。quota_applies=false（owner）→
  // 整段隐藏。用量条 fill = (used+reserved)/quota；满盘时提示联系管理员。
  function loadAccountStorage() {
    var el = els.acctPopStorage;
    if (!el) return;
    el.hidden = false;
    acctMeterHide(els.acctPopStorageBar, els.acctPopStorageFill);
    if (els.acctPopStorageValue) els.acctPopStorageValue.textContent = "";
    if (els.acctPopStorageDetail) {
      els.acctPopStorageDetail.textContent = t("acct.storage.loading");
    }
    apiFetch("/api/account/storage").then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        return { status: r.status, body: body };
      });
    }).then(function (res) {
      var data = res.body || {};
      if (res.status !== 200 || !data.quota_applies) {
        el.hidden = true;
        if (els.acctPopStorageValue) els.acctPopStorageValue.textContent = "";
        if (els.acctPopStorageDetail) els.acctPopStorageDetail.textContent = "";
        acctMeterHide(els.acctPopStorageBar, els.acctPopStorageFill);
        return;
      }
      var usedText = storageGbText(data.used_bytes);
      var quotaText = storageGbText(data.quota_bytes);
      if (usedText === null || quotaText === null) {
        if (els.acctPopStorageValue) els.acctPopStorageValue.textContent = "";
        if (els.acctPopStorageDetail) {
          els.acctPopStorageDetail.textContent = t("acct.storage.unavailable");
        }
        acctMeterHide(els.acctPopStorageBar, els.acctPopStorageFill);
        return;
      }
      if (els.acctPopStorageValue) {
        els.acctPopStorageValue.textContent = usedText + " / " + quotaText;
      }
      var usedBytes = acctBig(Math.round(Number(data.used_bytes)));
      var quotaBytes = acctBig(Math.round(Number(data.quota_bytes)));
      var reservedBytes = acctBig(Math.round(Number(data.reserved_bytes || 0))) || 0n;
      var filled = false;
      if (usedBytes !== null && quotaBytes !== null && quotaBytes > 0n) {
        filled = acctMeterPaint(els.acctPopStorageBar, els.acctPopStorageFill,
                                usedBytes + reservedBytes, quotaBytes);
      } else {
        acctMeterHide(els.acctPopStorageBar, els.acctPopStorageFill);
      }
      var full = usedBytes !== null && quotaBytes !== null &&
        (usedBytes + reservedBytes) >= quotaBytes;
      if (els.acctPopStorageDetail) {
        if (full) {
          els.acctPopStorageDetail.textContent = t("acct.storage.full");
        } else {
          var remainingBytes = Number(data.quota_bytes) -
            Number(data.used_bytes) - Number(data.reserved_bytes || 0);
          var remaining = storageGbText(Math.max(0, remainingBytes));
          els.acctPopStorageDetail.textContent = remaining === null ? "" :
            t("acct.storage.remaining", { remaining: remaining });
        }
      }
      if (!filled) acctMeterHide(els.acctPopStorageBar, els.acctPopStorageFill);
    }).catch(function () {
      if (els.acctPopStorageValue) els.acctPopStorageValue.textContent = "";
      if (els.acctPopStorageDetail) {
        els.acctPopStorageDetail.textContent = t("acct.storage.unavailable");
      }
      acctMeterHide(els.acctPopStorageBar, els.acctPopStorageFill);
    });
  }

  function loadAccountBalance() {
    if (els.acctPopRemaining) els.acctPopRemaining.textContent = t("acct.balance.loading");
    if (els.acctPopUnit) els.acctPopUnit.hidden = true;
    if (els.acctPopDetail) els.acctPopDetail.textContent = "";
    if (els.acctPopExtra) {
      els.acctPopExtra.textContent = "";
      els.acctPopExtra.hidden = true;
    }
    if (els.acctPopScope) els.acctPopScope.textContent = "";
    acctUsageMeterHide();
    if (els.acctTopupLink) els.acctTopupLink.hidden = true;
    // 统一入口：credits / legacy / owner 由 billing_backend 分流
    apiFetch("/api/account/credits").then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        return { status: r.status, body: body };
      });
    }).then(function (res) {
      if (res.status !== 200 || !res.body || !res.body.subject) {
        acctBalanceUnavailable(res.status, res.body && res.body.code);
        return;
      }
      acctBalanceRender(res.body);
    }).catch(function () {
      acctBalanceUnavailable(0, null);
    });
  }

  function openAccountSettings() {
    if (acctPopCtl) acctPopCtl.close();
    if (!sidebarCtrl) return;
    // 沿用既有改密/改绑入口：展开侧栏（手机开抽屉）并滚到侧栏账户区
    sidebarCtrl.expand();
    var target = els.changepwBtn || els.changeemailBtn;
    if (target && typeof target.scrollIntoView === "function") {
      try { target.scrollIntoView({ block: "center" }); }
      catch (e) { try { target.scrollIntoView(); } catch (e2) {} }
    }
  }

  var acctPopCtl = null;
  var annoPopCtl = null;
  var sharePopCtl = null;

  // els.tbbMore 的宿主：正式版在 #toolbar 内；浮层随工具栏横向滚动收起
  els.tbbMoreHost = function () { return els.tbbMore && els.tbbMore.parentNode; };

  function initToolbarPops() {
    acctPopCtl = bindToolbarPop(els.acctBtn, els.acctPop, {
      onOpen: function () { loadAccountBalance(); loadAccountStorage(); },
    });
    // 顶栏搜索浮层（§5.4）：打开时创建输入框并聚焦；Esc/外点关闭后焦点还按钮
    searchPopCtl = bindToolbarPop(els.tbSearchBtn, els.tbSearchPop, {
      onOpen: function () {
        ensureSearchInput();
        renderSearchResults();
        if (els.tbSearchBtn.setAttribute) els.tbSearchBtn.setAttribute("aria-expanded", "true");
        if (tbSearchState.input && typeof tbSearchState.input.focus === "function") {
          setTimeout(function () { try { tbSearchState.input.focus(); } catch (e) {} }, 30);
        }
      },
      onClose: function () {
        var doc = document;
        var inside = doc.activeElement && els.tbSearchPop && els.tbSearchPop.contains
          ? (els.tbSearchPop.contains(doc.activeElement) ||
             doc.activeElement === els.tbSearchBtn)
          : true;
        if (inside && els.tbSearchBtn && typeof els.tbSearchBtn.focus === "function") {
          try { els.tbSearchBtn.focus(); } catch (e) { /* 忽略 */ }
        }
      },
    });
    // 顶栏分享浮层（§5.1）：默认分享对象=当前打开的切片；打开时刷新列表
    sharePopCtl = bindToolbarPop(els.tbShareBtn, els.tbSharePop, {
      onOpen: function () {
        if (!sharePendingSlides && state.slide) sharePendingSlides = [activeSlideRef()];
        updateShareTargetLine();
        if (allSharesCache) renderShareList(allSharesCache);
        els.shareResult.style.display = els.shareResult.style.display || "none";
      },
      onClose: function () {
        if (els.tbShareBtn && typeof els.tbShareBtn.focus === "function") {
          // 仅当焦点在浮层内（Esc 路径）才归还按钮；外点关闭不抢焦点
          var doc = document;
          if (doc.activeElement && els.tbSharePop.contains && els.tbSharePop.contains(doc.activeElement)) {
            try { els.tbShareBtn.focus(); } catch (e) { /* 忽略 */ }
          }
        }
      },
    });
    if (els.tbSearchClose) {
      els.tbSearchClose.addEventListener("click", function () { closeSearchPop(); });
    }
    if (els.tbShareClose) {
      els.tbShareClose.addEventListener("click", function () { if (sharePopCtl) sharePopCtl.close(); });
    }
    annoPopCtl = bindToolbarPop(els.annoMoreBtn, els.annoPop, {
      onOpen: function () {
        var input = els.annoLabelInput;
        if (input && typeof input.focus === "function") {
          setTimeout(function () { try { input.focus(); } catch (e) {} }, 30);
        }
      },
    });
    if (els.acctSettingsBtn) {
      els.acctSettingsBtn.addEventListener("click", openAccountSettings);
    }
    // 矩形设置 popover：开合由工具激活状态驱动（toggleRectTool/exitRoi）；
    // 这里只补「点击外部关闭 popover（工具保持激活）」的语义。
    document.addEventListener("click", function (e) {
      if (!rectToolActive() || !els.roiSettings || els.roiSettings.hidden) return;
      var tgt = e.target;
      if (tgt && tgt.closest &&
          (tgt.closest("#roi-settings") || tgt.closest("#roi-rect-btn"))) return;
      els.roiSettings.hidden = true;
      els.roiRectBtn.setAttribute("aria-expanded", "false");
    });
    toolbarPopClosers.push(function () {
      if (rectToolActive() && els.roiSettings && !els.roiSettings.hidden) {
        els.roiSettings.hidden = true;
        els.roiRectBtn.setAttribute("aria-expanded", "false");
      }
    });
    window.addEventListener("resize", function () {
      if (rectToolActive() && els.roiSettings && !els.roiSettings.hidden) {
        els.roiSettings.hidden = true;
        els.roiRectBtn.setAttribute("aria-expanded", "false");
      }
    });
  }

  // ---------- 宽度断点分组（§3.3） ----------
  // >=1440 全展开；1024–1439 折「视图」组（旋转/镜像/画质/通道）与「标注」组；
  // <1024 只留当前工具、AI、倍率、账户，其余入 ⋯。<=768 交还既有移动端布局
  //（底栏/上下文条 CSS 定位，低频组同样折叠）。搬移真实 DOM 节点（监听器/状态机不复制），
  // 折叠目标统一是现有 #tbb-more 菜单。
  var TB_FOLD_SPECS = [
    { id: "view-tools-group", tiers: ["mid", "narrow", "mobile"] },
    { id: "quality-control", tiers: ["mid", "narrow", "mobile"] },
    { id: "channel-btn", tiers: ["mid", "narrow", "mobile"] },
    { id: "anno-tools-group", tiers: ["mid", "narrow", "mobile"] },
    { id: "anno-btn", tiers: ["mid", "narrow", "mobile"] },
    { id: "save-anno-btn", tiers: ["mid", "narrow", "mobile"] },
    { id: "zoom-group", tiers: ["narrow", "mobile"] },
    { id: "save-btn", tiers: ["narrow", "mobile"] },
    { id: "mpp-setter", tiers: ["narrow", "mobile"] },
    { id: "zoom-native", tiers: ["narrow", "mobile"] },
  ];

  function tbTierForWidth() {
    try {
      if (window.matchMedia) {
        if (window.matchMedia("(min-width: 1440px)").matches) return "wide";
        if (window.matchMedia("(min-width: 1024px)").matches) return "mid";
        return "narrow";
      }
    } catch (e) { /* fallthrough */ }
    var w = Number(window.innerWidth) || 1440;
    if (w >= 1440) return "wide";
    if (w >= 1024) return "mid";
    return "narrow";
  }

  // narrow 档「只留当前工具」：绘制工具激活时其所在组保留在主行
  function tbSpecPinned(id) {
    if (id === "anno-tools-group" && state.drawMode && window.innerWidth > 768) return true;
    return false;
  }

  function tbMoveIntoMore(el, more) {
    if (el.parentNode === more) return;
    el.__tbHome = { parent: el.parentNode, next: el.nextSibling };
    more.appendChild(el);
  }

  function tbRestore(el) {
    var home = el.__tbHome;
    if (!home || !home.parent) return;
    try { home.parent.insertBefore(el, home.next); }
    catch (e) { try { home.parent.appendChild(el); } catch (e2) {} }
  }

  var tbCurrentTier = null;

  // 改版 2026-10-08：「搜索/分享」两个新按钮的溢出收放——不进静态档位表，
  // 每次布局后按 #toolbar 实测溢出（scrollWidth vs clientWidth）套用收放步骤。
  // 改版第四轮 §1.2：搜索/分享/矩形/箭头/描图已是常驻图标按钮（不再有
  // 「文字→图标」过渡档，原 iconStep 移除）；实测收放只剩「折入 ⋯」，
  // 优先级不变——低频控件先让位（原始 RGB 标识 → mpp → 1:1 → 保存标记 →
  // 标准/精细 → 镜像/视图组），最后万不得已才折 分享、搜索。
  // AI 与账户 chip 永不折叠。迟滞式回退（不溢出才从最后一步起逐步复原，
  // 每步复测；applied 恒为步骤前缀）防抖动；openSlide 成功/清理路径延时
  // 重测 + #toolbar ResizeObserver 捕捉画质/RGB/mpp 控件异步出现。
  var tbFoldSteps = null;

  function tbBuildFoldSteps() {
    var more = els.tbbMore;
    function foldStep(id) {
      return {
        applied: false,
        apply: function () {
          var el = document.getElementById(id);
          if (!el || more.contains(el)) return; // 已在 ⋯（档位表折过/无内容）
          el.__tbOverflowFolded = true;
          tbMoveIntoMore(el, more);
        },
        revert: function () {
          var el = document.getElementById(id);
          if (!el || !el.__tbOverflowFolded) return;
          tbRestore(el);
          el.__tbOverflowFolded = false;
        },
      };
    }
    return (tbFoldSteps = [
      foldStep("rgb-badge"),
      foldStep("mpp-setter"),
      foldStep("zoom-native"),
      foldStep("save-anno-btn"),
      foldStep("quality-control"),
      foldStep("flip-btn"),
      foldStep("view-tools-group"),
      foldStep("tb-share-btn"),
      foldStep("tb-search-btn"),
    ]);
  }

  function tbApplyOverflowFold() {
    var more = els.tbbMore;
    if (!more || more.classList.contains("open")) return;
    var steps = tbBuildFoldSteps();
    var toolbar = more.parentNode;
    if (!toolbar || !toolbar.clientWidth || !toolbar.scrollWidth) return;
    function overflowPx() { return toolbar.scrollWidth - toolbar.clientWidth; }
    var guard = 0;
    // 仍溢出：按优先级套用下一步
    while (overflowPx() > 1 && guard < 12) {
      guard += 1;
      var next = null;
      for (var s = 0; s < steps.length; s++) {
        if (!steps[s].applied) { next = steps[s]; break; }
      }
      if (!next) break;
      next.apply();
      next.applied = true;
    }
    // 不溢出：从最后一步起回退，每退一步复测（迟滞；applied 恒为前缀）
    for (var j = steps.length - 1; j >= 0; j--) {
      if (!steps[j].applied) continue;
      steps[j].revert();
      if (overflowPx() > 1) {
        steps[j].apply();
        break;
      }
      steps[j].applied = false;
    }
  }

  function applyToolbarTier() {
    var more = els.tbbMore;
    if (!more) return;
    var tier = tbTierForWidth();
    // <=768：底栏使用 44px 命中区，低频组收进更多菜单，搜索/分享/账户优先保留
    try {
      if (window.matchMedia && window.matchMedia(SB_MOBILE_QUERY).matches) tier = "mobile";
    } catch (e) {}
    TB_FOLD_SPECS.forEach(function (spec) {
      var el = document.getElementById(spec.id);
      if (!el) return;
      var wantFolded = spec.tiers.indexOf(tier) >= 0 && !tbSpecPinned(spec.id);
      // 溢出收放机制持有的节点不在此归位（它在不溢出时自行放开）
      if (!wantFolded && el.__tbOverflowFolded) return;
      var isFolded = el.parentNode === more;
      if (wantFolded === isFolded) return;
      if (wantFolded) tbMoveIntoMore(el, more);
      else tbRestore(el);
    });
    tbApplyOverflowFold();
    // 档位类名（幂等）：CSS 据此隐藏悬空分隔线等
    var toolbar = more.parentNode;
    if (toolbar && toolbar.classList) {
      toolbar.classList.toggle("tb-tier-mid", tier === "mid");
      toolbar.classList.toggle("tb-tier-narrow", tier === "narrow");
    }
    if (tier !== tbCurrentTier) {
      tbCurrentTier = tier;
      // 档位切换时收起已开的工具栏浮层（几何已失效）
      if (annoPopCtl) annoPopCtl.close();
      if (acctPopCtl) acctPopCtl.close();
      if (searchPopCtl) searchPopCtl.close();
      if (sharePopCtl) sharePopCtl.close();
      if (rectToolActive() && els.roiSettings && !els.roiSettings.hidden) {
        els.roiSettings.hidden = true;
        els.roiRectBtn.setAttribute("aria-expanded", "false");
      }
    }
  }

  function initToolbarTier() {
    applyToolbarTier();
    window.addEventListener("resize", function () { applyToolbarTier(); });
    // 上下文控件（画质段/RGB 标识/mpp）随切片异步出现/消失 → 内容尺寸变化
    // 时重测溢出（ResizeObserver；收放为迟滞式，反复触发稳定不振荡）
    try {
      var toolbar = els.tbbMore && els.tbbMore.parentNode;
      if (toolbar && typeof ResizeObserver === "function") {
        new ResizeObserver(function () {
          try { applyToolbarTier(); } catch (e) { /* 忽略 */ }
        }).observe(toolbar);
      }
    } catch (e) { /* 老环境无 ResizeObserver：resize/slice-open 兜底 */ }
  }

  function bindEvents() {
    els.zoomIn.addEventListener("click", zoomIn);
    els.zoomOut.addEventListener("click", zoomOut);
    if (els.zoomNative) els.zoomNative.addEventListener("click", zoomNative);
    els.rotateBtn.addEventListener("click", rotate);
    els.flipBtn.addEventListener("click", flip);
    els.resetBtn.addEventListener("click", reset);
    els.saveBtn.addEventListener("click", saveCrop);
    els.saveAnnoBtn.addEventListener("click", saveAnno);
    els.mppSetBtn.addEventListener("click", setMpp);
    els.mppInput.addEventListener("keydown", function (e) { if (e.key === "Enter") setMpp(); });

    // 退出登录：POST /logout + CSRF（docs §10.14）
    if (els.logoutBtn) { els.logoutBtn.addEventListener("click", doLogout); }

    // W4/W6：侧栏主入口 = 导入抽屉；#file-input 保持隐藏（抽屉内/拖拽仍触发）
    if (els.importSlidesBtn) {
      els.importSlidesBtn.addEventListener("click", function () {
        openImportDrawer(els.importSlidesBtn);
      });
    }
    if (els.uploadBtn) {   // 兼容：旧模板若仍渲染 #upload-btn，保持直开文件框
      els.uploadBtn.addEventListener("click", function () { els.fileInput.click(); });
    }
    els.fileInput.addEventListener("change", function () {
      if (this.files && this.files[0]) { uploadFile(this.files[0]); this.value = ""; }
    });

    // 标注
    els.annoBtn.addEventListener("click", function () {
      if (annoPanelOpen) { closeAnnoPanel(); } else { openAnnoPanel(); }
    });
    els.annoPanelClose.addEventListener("click", closeAnnoPanel);
    els.annoAllBtn.addEventListener("click", toggleAnnoAll);
    // 面板头部「显示全部标记」切换钮（与 toggleAnnoAll 同一逻辑）
    if (els.annoAllToggle) els.annoAllToggle.addEventListener("click", toggleAnnoAll);
    if (els.annoExportBtn) els.annoExportBtn.addEventListener("click", exportAnnotations);
    syncAnnoExportBtn();
    els.annoArrowBtn.addEventListener("click", function () { toggleDrawMode("arrow"); });
    els.annoFreeBtn.addEventListener("click", function () { toggleDrawMode("freehand"); });
    // 升级 C：单一矩形入口 + 紧凑设置区（旧 6/6.5 分段/滑块已移除）
    if (els.roiRectBtn) {
      els.roiRectBtn.addEventListener("click", toggleRectTool);
    }
    if (els.roiUnitSelect) {
      els.roiUnitSelect.addEventListener("change", function () {
        state.roiUnit = els.roiUnitSelect.value;
        syncRoiSettings();
        updateRoiSummary();
      });
    }
    if (els.roiLockRatio) {
      els.roiLockRatio.addEventListener("change", function () {
        state.roiLockRatio = !!els.roiLockRatio.checked;
      });
    }
    if (els.roiPresetSelect) {
      els.roiPresetSelect.addEventListener("change", function () {
        state.roiPreset = els.roiPresetSelect.value;
        if (!state.roiPreset) return;
        // 预设只填入宽高数值（mm），不强制永远锁成正方形（§6.1）
        state.roiUnit = "mm";
        if (els.roiUnitSelect) els.roiUnitSelect.value = "mm";
        els.roiWInput.value = state.roiPreset;
        els.roiHInput.value = state.roiPreset;
        applyRectInputs();
      });
    }
    [els.roiWInput, els.roiHInput].forEach(function (inp) {
      if (!inp) return;
      inp.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); applyRectInputs(); }
      });
      inp.addEventListener("change", applyRectInputs);
    });

    // 标注画布层绘制事件
    var c = els.annoCanvas;
    c.addEventListener("pointerdown", onAnnoPointerDown);
    c.addEventListener("pointermove", onAnnoPointerMove);
    c.addEventListener("pointerup", onAnnoPointerUp);
    // 工单 D：pointercancel ≠ pointerup——取消恢复，绝不触发完成/保存
    c.addEventListener("pointercancel", onAnnoPointerCancel);
    // 矩形手势兜底：窗口失焦（切标签/最小化）= 收尾恢复，绝不按完成保存
    window.addEventListener("blur", cancelRectPointerSessions);
    // 工单 E：查看器右键菜单（preventDefault + 冻结载荷的附件意图）
    c.addEventListener("contextmenu", onViewerContextMenu);
    // §6.2：viewer 容器「捕获阶段」对象路由——只在确认对象编辑时消费事件，
    // 其余交给 OSD 平移（不给透明画布开常驻 pointer-events:auto）。
    // （最小 DOM 桩可能没有 container.addEventListener：真实浏览器恒有）
    if (viewer && viewer.container &&
        typeof viewer.container.addEventListener === "function") {
      var vc = viewer.container;
      vc.addEventListener("pointerdown", routeObjectPointerDown, true);
      vc.addEventListener("pointermove", routeObjectPointerMove, true);
      vc.addEventListener("pointerup", routeObjectPointerUp, true);
      vc.addEventListener("pointercancel", routeObjectPointerCancel, true);
      vc.addEventListener("dblclick", routeObjectDblClick, true);
      // count_cells 高亮的用户清除：非绘制态下「点击」方框内部（按下/抬起
      // 位移 ≤6px 的屏幕像素）即清除；不消费事件，平移/对象编辑照常。
      var countClickStart = null;
      vc.addEventListener("pointerdown", function (e) {
        countClickStart = (countHighlight && state.drawMode == null)
          ? { x: e.clientX, y: e.clientY } : null;
      }, true);
      vc.addEventListener("pointerup", function (e) {
        if (!countClickStart || !countHighlight) return;
        var moved = Math.hypot(e.clientX - countClickStart.x,
                               e.clientY - countClickStart.y);
        countClickStart = null;
        if (moved > 6) return;
        try {
          var cpt = screenToImg(e);
          var cb = countHighlight;
          if (cpt && cpt.x >= cb.x && cpt.x <= cb.x + cb.w &&
              cpt.y >= cb.y && cpt.y <= cb.y + cb.h) {
            clearCountHighlight();
          }
        } catch (err) { /* 坐标换算失败忽略 */ }
      }, true);
    }
    // count_cells 结果高亮的响应流 tap（AI 路径 fetch；插件加载在后，看到的
    // 是包装后的 fetch）。
    installCountHighlightTap();
    // §6.2：Space 平移（输入框外）——按住即永远平移，即使指针经过对象
    window.addEventListener("keydown", onSpaceKeyForPan, true);
    window.addEventListener("keyup", onSpaceKeyForPan, true);
    // 箭头单击-起点模式：双击终点完成（与第二次单击同效，防御性兜底）
    c.addEventListener("dblclick", function (e) {
      if (state.drawMode === "arrow" && drawPreview && drawPreview.type === "arrow" &&
          drawPreview.armed && validPreviewGeom(drawPreview)) {
        e.preventDefault();
        finishDraw();
      }
    });
    window.addEventListener("resize", function () { resizeAnnoCanvas(); redrawAnnoCanvas(); });
    // 工单 D：查看器级键盘——Escape 取消绘制/选区、Enter 提交有效未提交草稿、
    // Ctrl/Cmd+Z 撤销、Ctrl/Cmd+Shift+Z（及 Ctrl+Y）重做。
    // 输入框/textarea/contenteditable 内保留原生行为（含原生文字撤销）。
    window.addEventListener("keydown", onViewerKeydown);

    // 侧栏开合（升级 A）：菜单按钮切换（桌面=收起/展开、手机=抽屉）、
    // 遮罩点击关闭、Escape 关闭手机抽屉
    if (els.menuBtn) {
      els.menuBtn.addEventListener("click", function () { sidebarCtrl.toggle(); });
    }
    if (els.sidebarMask) {
      els.sidebarMask.addEventListener("click", function () { sidebarCtrl.closeDrawer(); });
    }
    document.addEventListener("keydown", function (e) {
      if (e && e.key === "Escape" && sidebarCtrl.isMobile() && sidebarCtrl.isDrawerOpen()) {
        sidebarCtrl.closeDrawer();
      }
    });

    // 无切片空态「选择切片」：展开侧栏并聚焦搜索框（§4.1）
    if (els.viewerEmptyPick) {
      els.viewerEmptyPick.addEventListener("click", function () {
        sidebarCtrl.expandAndFocusSearch();
      });
    }
    // 主页升级 H3：空态「上传切片」复用既有导入抽屉（同一上传管线
    // 与能力判定，不在空态另做第二套上传或权限判断）
    if (els.viewerEmptyUpload) {
      els.viewerEmptyUpload.addEventListener("click", function () {
        openImportDrawer(els.viewerEmptyUpload);
      });
    }
    // 移动端 ⋯ 溢出面板（AI 读片 + 缩放徽章）；§3.3 宽度分组的统一折叠目标
    bindTbbMore();
    // 工具栏浮层（账户/标注选项/矩形设置外点关闭）：§3.3/§3.5
    initToolbarPops();

    // 新建项目（W3：对话框；empty 永远空草稿，selection 复制勾选快照）
    if (els.newProjectBtn) {
      els.newProjectBtn.addEventListener("click", function () {
        openProjectDialog("empty", null, els.newProjectBtn);
      });
    }
    if (els.pcdClose) els.pcdClose.addEventListener("click", closeProjectDialog);
    if (els.pcdCancel) els.pcdCancel.addEventListener("click", closeProjectDialog);
    if (els.pcdConfirm) els.pcdConfirm.addEventListener("click", submitProjectDialog);
    if (els.pcdName) {
      els.pcdName.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          e.preventDefault();
          if (els.pcdNote && typeof els.pcdNote.focus === "function") {
            try { els.pcdNote.focus(); } catch (err) {}
          }
        }
      });
    }
    if (els.pcdNote) {
      els.pcdNote.addEventListener("keydown", function (e) {
        // R4：Enter 与确认按钮共用同一提交锁（inFlight 双保险）
        if (e.key === "Enter") { e.preventDefault(); submitProjectDialog(); }
      });
    }
    if (els.projectCreateMask) {
      els.projectCreateMask.addEventListener("click", function (e) {
        if (e.target === els.projectCreateMask) closeProjectDialog();
      });
    }

    // 导入抽屉（W4/W6）
    if (els.importDrawerClose) {
      els.importDrawerClose.addEventListener("click", closeImportDrawer);
    }
    if (els.importDrawerMask) {
      els.importDrawerMask.addEventListener("click", closeImportDrawer);
    }
    if (els.importTabLocal) {
      els.importTabLocal.addEventListener("click", function () { switchImportTab("local"); });
    }
    if (els.importTabBaidu) {
      els.importTabBaidu.addEventListener("click", function () { switchImportTab("baidu"); });
    }
    if (els.importPickFiles) {
      els.importPickFiles.addEventListener("click", function () {
        if (els.fileInput) els.fileInput.click();
      });
    }
    // 「选择文件夹（MRXS）」（webkitdirectory）→ MRXS 完整包走面板队列
    //（与拖入目录同一 importBundleRows 分流）
    if (els.importPickFolder) {
      els.importPickFolder.addEventListener("click", function () {
        if (els.importFolderInput) els.importFolderInput.click();
      });
    }
    if (els.importFolderInput) {
      els.importFolderInput.addEventListener("change", importPickFolder);
    }
    if (els.importDropZone) {
      // 抽屉内大拖放区点击 = 打开文件选择（拖放本身由 document 级处理）
      els.importDropZone.addEventListener("click", function () {
        if (els.fileInput) els.fileInput.click();
      });
    }
    if (els.importTargetSelect) {
      els.importTargetSelect.addEventListener("change", function () {
        // folder-experience §1：用户已操作目标 → 本次导入会话 dirty，
        // 迟到的项目列表响应不得覆盖其选择
        importDrawerState.targetDirty = true;
        syncImportTargetFromSelect();
      });
    }
    if (els.baiduTargetSelect) {
      els.baiduTargetSelect.addEventListener("change", function () {
        importDrawerState.targetDirty = true;   // 百度页签同样保护手动选择
      });
    }
    if (els.importTargetNewName) {
      els.importTargetNewName.addEventListener("input", function () {
        importDrawerState.targetDirty = true;
        importTargetState.newProjectName = (els.importTargetNewName.value || "").trim();
      });
    }

    // 申请新格式支持（抽屉内次级入口；提交逻辑在 submitFormatRequest）
    if (els.formatReqBtn && els.formatReqForm) {
      els.formatReqBtn.addEventListener("click", function () {
        els.formatReqForm.hidden = !els.formatReqForm.hidden;
      });
      els.frCancel.addEventListener("click", function () {
        if (els.frSubmit.disabled) return;
        formatRequestEntry = null;
        els.formatReqForm.hidden = true;
      });
      els.frSubmit.addEventListener("click", submitFormatRequest);
    }

    // 百度分享页签（W5/W6）
    if (els.baiduListBtn) {
      els.baiduListBtn.addEventListener("click", startBaiduEnumeration);
    }
    if (els.baiduSearch) {
      els.baiduSearch.addEventListener("input", renderBaiduCandidates);
    }
    if (els.baiduPrevBtn) {
      els.baiduPrevBtn.addEventListener("click", function () {
        if (!baiduState.prevStack.length) return;
        baiduState.cursor = baiduState.prevStack.pop();
        loadBaiduCandidates();
      });
    }
    if (els.baiduNextBtn) {
      els.baiduNextBtn.addEventListener("click", function () {
        if (!baiduState.nextCursor) return;
        if (baiduState.cursor) baiduState.prevStack.push(baiduState.cursor);
        baiduState.cursor = baiduState.nextCursor;
        loadBaiduCandidates();
      });
    }
    if (els.baiduImportBtn) {
      els.baiduImportBtn.addEventListener("click", startBaiduImport);
    }

    // Esc 关闭：项目对话框优先，其次导入抽屉；Tab 圈闭焦点
    document.addEventListener("keydown", function (e) {
      if (projectDialog.open) { handleProjectDialogKeydown(e); return; }
      if (!importDrawerState.open) return;
      if (e.key === "Escape") {
        e.preventDefault();
        closeImportDrawer();
      } else if (e.key === "Tab") {
        trapDrawerFocus(e);
      }
    });

    // 文件夹浏览器（改版 2026-10-08 §5.1–5.3）：返回 / ＋ 菜单 / 文件夹 ⋯ 菜单 / 翻页
    initFbBrowser();


    // 分享
    els.shareExpiresSelect.addEventListener("change", function () {
      els.shareExpiresCustom.style.display = this.value === "custom" ? "inline-block" : "none";
    });
    els.shareCreateBtn.addEventListener("click", onShareCreateClick);
    els.shareResultCopy.addEventListener("click", function () { copyText(els.shareResultUrl.value); });
    // 「选择切片…」：复用切片选择器多选（§5.1）；确认后回填浮层目标
    if (els.sharePickBtn) {
      els.sharePickBtn.addEventListener("click", function () {
        openSlidePicker(null, null, { mode: "share" });
      });
    }

    // 修改我的密码（owner/user 通用；docs §8.1）
    initChangePw();

    // 更换邮箱（P1-3 身份收口 review-2026-09-08 P2-2；与改密同级账户入口）
    initChangeEmail();

    // 数据共享（P2 账户设置 §3.5；与改密/改绑同级的自愿研究授权入口）
    initDataShare();

    // 反馈问题对话框（改版第四轮 §3；侧栏底部 + 账户弹层双入口）
    initFeedbackDialog();

    // user max_steps 只读同步（AI 预算管理 UI 已迁入 admin 插件，PR5）
    initAiMaxStepsSync();

    // 切片选择器
    els.pickerClose.addEventListener("click", closeSlidePicker);
    els.pickerConfirm.addEventListener("click", confirmSlidePicker);
    els.pickerMask.addEventListener("click", function (e) {
      if (e.target === els.pickerMask) closeSlidePicker();
    });

    // AI 读片助手：点击交由插件处理（发 panel.toggle）。插件未启用时 aiBtn 不渲染。
    if (els.aiBtn) {
      els.aiBtn.addEventListener("click", function () {
        hpRequest("panel.toggle", {}).catch(function () { /* 插件未启用：静默 */ });
      });
    }
  }

  // =========================================================================
  // HistoPilot HostBridge host 适配（Stage 2：同源同窗口）
  // -------------------------------------------------------------------------
  // 平台 host：把 viewer/state/selection/annotation 能力经 HostBridge 暴露给插件，
  // 并把插件的 notification/annotation/panel 事件转回平台动作。插件缺失（flag 关闭）时
  // 全部 hp* 调用静默降级，人工读片不受影响（Stage 2 验收项）。
  function hpReady() {
    return !!(window.HostBridgeHost && window.HistoPilot);
  }
  // Host→Plugin event（单向）
  function hpEmit(type, payload) {
    try { if (hpReady()) window.HostBridgeHost.emit(type, payload); } catch (e) {}
  }
  // Host→Plugin request（Promise；插件未启用时 reject，调用方自行 catch）
  function hpRequest(method, payload) {
    if (!hpReady()) return Promise.reject({ code: "plugin_disabled" });
    try { return window.HostBridgeHost.request(method, payload); }
    catch (e) { return Promise.reject({ code: "plugin_disabled" }); }
  }

  // stale 比对（P2 合同 §5.5）：请求携带的 slide 标识按 id 比对；旧插件载荷
  // 只带 name 的兼容分支——按 name 解析比对（退役条件：P6 插件侧全面切 id
  // 后删除 name 分支）。宽容缺省：不带 slide 时不拒绝。
  function bridgeSlideStale(p) {
    if (!state.slide || p.slide == null || p.slide === "") return false;
    var ref = String(p.slide);
    if (state.slide.id && ref === String(state.slide.id)) return false;
    if (ref === String(state.slide.name)) return false;
    return true;
  }

  // 注册 host 侧能力（在 init 中调用，viewer 在 initViewer 后才就绪）
  function registerHostBridgeHandlers() {
    var host = window.HostBridgeHost;
    if (!host) return;
    // 通用插件权限门（Stage 5-2）：每个被 gate 的 host 方法入口先查
    // env.pluginInstallationId。未知 ID fail-closed；histopilot 仅因在
    // PRIVILEGED_PLUGIN_IDS 显式名单中才放行（不能靠「不在权限表」冒充）。
    // 同窗口执行仍不是安全边界（插件可触达 host 全局）；iframe sandbox 另做。
    // 用法：gate(method, fn(payload, env))，把 fn 包成 fn(payload, env) → 先 gate 再执行业务。
    function gate(method, fn) {
      return function (payload, env) {
        var pluginId = env && env.pluginInstallationId;
        var pp = window.PluginPermissions;
        if (pp && pp.gatePermission) {
          var denied = pp.gatePermission(pluginId, method, window.SVS_PLUGIN_PERMISSIONS);
          if (denied) throw denied;
        } else if (pluginId !== "histopilot") {
          throw { code: "permission_denied", message: "未知插件身份", retryable: false };
        }
        return fn(payload, env);
      };
    }
    // 握手期 bridge.negotiate 不在此注册：host-bridge.js 路由器原生应答（2026-08-16
    // 修复）——插件脚本先于 app.js 加载并立即握手时，等这里的 onRequest 注册会先
    // 收到 unknown_method（demo 实测）。业务方法才走下方注册表。
    // Plugin→Host request（被 gate 的方法：slide.getCurrent / selection.getBbox /
    // viewer.navigate / viewer.getViewport / viewer.highlight /
    // viewer.applyRenderContext / annotation.create / annotation.read /
    // annotation.focus）
    host.onRequest("slide.getCurrent", gate("slide.getCurrent", function () {
      if (!state.slide) return null;
      // P2 合同 §5.5：返回含 id（slide_id 操作键）+ name/display_name 快照
      return { id: state.slide.id || null, name: state.slide.name,
               display_name: state.slide.display_name || null,
               width: state.slide.width, height: state.slide.height,
               mppX: state.slide.mppX, mppY: state.slide.mppY };
    }));
    host.onRequest("selection.getBbox", gate("selection.getBbox", function () { return currentSelectionBbox(); }));
    host.onRequest("viewer.getSkillTarget", gate("viewer.getSkillTarget", function () {
      // E3 描绘技能（viewer-drawing-skill-contract §2.1 target）：返回发送时
      // 冻结的技能目标——优先级：选中标注 > 选框 ROI > 当前视野；含 slide_id /
      // asset_revision 与 annotation 元数据。只读语义：只读几何读取，无副作用；
      // 无切片 / 临时查看切片（只读视图）→ available:false + reason——插件据此
      // 在 @ 候选显示「暂不可用」说明并引导到有编辑权限的工作台，不发写意图。
      return currentSkillTarget();
    }));
    host.onRequest("viewer.getViewport", gate("viewer.getViewport", function () {
      // P1「普通发送绑定浏览器当前视野」：返回当前 OpenSeadragon 视野的
      // level-0 像素 bbox {x,y,w,h}，供插件随 run/continue 发送附带，使
      // 「分析/判读当前视野」类指令落到用户真实在看的范围（而非 AI 上次
      // 快照）。R1（同 viewer.navigate 惯例）：viewer 未就绪回真实 error
      // code（retryable），不吞异常伪报 ok；无切片返回 null。
      if (!state.slide) return null;
      if (!viewer || !viewer.viewport) {
        throw { code: "viewer_not_ready", message: "查看器未就绪", retryable: true };
      }
      var bb = level0ViewportBbox();
      if (!bb) {
        throw { code: "invalid_geometry", message: "视野几何非法", retryable: false };
      }
      return bb;
    }));
    host.onRequest("viewer.navigate", gate("viewer.navigate", function (p) {
      // AI goto/snapshot 跳转：level-0 bbox → viewport.fitBounds。
      // （文档 {x,y,level} 在本阶段以 level-0 bbox 表达，agent 全程在图像坐标系工作）
      // R1（2026-09-05）：viewer 未就绪/几何非法回真实 error code，不再吞异常
      // 仍报 ok:true；请求携带 slide 标识且与当前切片不符（过期操作）时拒绝
      // （宽容缺省：旧插件不带 slide 时不拒绝；stale 比对按 id，见
      // bridgeSlideStale——旧插件 name 载荷走兼容分支）。
      p = p || {};
      if (bridgeSlideStale(p)) {
        throw { code: "stale_slide", message: "导航请求属于另一切片", retryable: false };
      }
      if (!viewer || !viewer.viewport) {
        throw { code: "viewer_not_ready", message: "查看器未就绪", retryable: true };
      }
      var nx = Number(p.x), ny = Number(p.y), nw = Number(p.w), nh = Number(p.h);
      if (!isFinite(nx) || !isFinite(ny) || !isFinite(nw) || !isFinite(nh) || nw <= 0 || nh <= 0) {
        throw { code: "invalid_geometry", message: "导航几何非法", retryable: false };
      }
      // 新 goto：count_cells 计数范围高亮是「当前快照」的测量展示，导航即过期
      clearCountHighlight();
      viewer.viewport.fitBounds(
        viewer.viewport.imageToViewportRectangle(nx, ny, nw, nh));
      return { ok: true };
    }));
    host.onRequest("viewer.highlight", gate("viewer.highlight", function (p) {
      // 插件叠加层：写入平台 aiOverlay 并重绘画布（替代插件直接写 aiOverlay/redrawAnnoCanvas）。
      // R1：逐框几何校验，非法回 invalid_geometry；slide 标识不符拒绝过期画框
      // （宽容缺省：旧插件不带时不拒绝；stale 比对按 id，见 bridgeSlideStale）。
      p = p || {};
      if (bridgeSlideStale(p)) {
        throw { code: "stale_slide", message: "画框请求属于另一切片", retryable: false };
      }
      var boxes = Array.isArray(p.boxes) ? p.boxes : [];
      for (var bi = 0; bi < boxes.length; bi++) {
        var bb = boxes[bi] || {};
        var bx = Number(bb.x), by = Number(bb.y), bw = Number(bb.w), bh = Number(bb.h);
        if (!isFinite(bx) || !isFinite(by) || !isFinite(bw) || !isFinite(bh) || bw <= 0 || bh <= 0) {
          throw { code: "invalid_geometry", message: "叠加框几何非法", retryable: false };
        }
      }
      aiOverlay = boxes;
      redrawAnnoCanvas();
      return { ok: true };
    }));
    // 升级 E §8.2-3：历史配色恢复——插件把已持久化的 wire render_context
    //（通道配置，无短期令牌）发回平台，由平台现有通道控制器经公开 setter
    // 应用并按既有管线刷新显示令牌（只作用于人眼 Viewer，不触碰模型绑定）。
    // 校验：slide 匹配、fingerprint 形态、通道 1..8、index/颜色合法；不通过
    // 回 {ok:true, applied:false}，插件侧据此显示「历史配色未知」，不伪称一致。
    host.onRequest("viewer.applyRenderContext", gate("viewer.applyRenderContext", function (p) {
      p = p || {};
      // stale 比对按 id（见 bridgeSlideStale；旧插件 name 载荷走兼容分支）
      if (bridgeSlideStale(p)) {
        throw { code: "stale_slide", message: "配色恢复请求属于另一切片", retryable: false };
      }
      var ctx = p.render_context;
      if (!ctx || !ctx.fingerprint || !Array.isArray(ctx.active_channels) || !ctx.active_channels.length) {
        return { ok: true, applied: false, reason: "no_context" };
      }
      if (!/^[0-9a-f]{64}$/i.test(String(ctx.fingerprint))) {
        return { ok: true, applied: false, reason: "bad_fingerprint" };
      }
      var chans = ctx.active_channels;
      if (chans.length > 8) return { ok: true, applied: false, reason: "too_many_channels" };
      for (var ci = 0; ci < chans.length; ci++) {
        var ch = chans[ci] || {};
        if (!Number.isInteger(ch.index) || ch.index < 0) return { ok: true, applied: false, reason: "bad_channel" };
        if (typeof ch.color !== "string" || !/^#[0-9a-fA-F]{6}$/.test(ch.color)) {
          return { ok: true, applied: false, reason: "bad_color" };
        }
      }
      if (!channelCtrl || !channelCtrl.isMultichannel || !channelCtrl.isMultichannel()) {
        return { ok: true, applied: false, reason: "not_multichannel" };
      }
      // 平台当前显示已是同一 context（fingerprint 一致）→ 无需恢复。
      var curFp = (channelCtrl.getFingerprint && channelCtrl.getFingerprint()) || null;
      if (curFp && String(curFp).toLowerCase() === String(ctx.fingerprint).toLowerCase()) {
        return { ok: true, applied: false, reason: "already_current" };
      }
      var wanted = {};
      chans.forEach(function (ch) { wanted[ch.index] = ch; });
      // 应用顺序（受服务端 1..8 约束）：先关掉不在目标的通道（至少保留 1 个，
      // 剩余的等目标通道激活后再关），再激活目标通道，最后逐通道套历史颜色
      //（经 setChannelColor 的 hex 校验）。任一通道被平台拒绝即视为未完全
      // 应用 → 插件侧显示「历史配色未知」，不伪称一致。
      var applied = true;
      (channelCtrl.selection || []).slice().forEach(function (idx) {
        if (!wanted[idx] && (channelCtrl.selection || []).length > 1) {
          if (!channelCtrl.setChannelActive(idx, false)) applied = false;
        }
      });
      chans.forEach(function (ch) {
        if (!channelCtrl.setChannelActive(ch.index, true)) applied = false;
      });
      (channelCtrl.selection || []).slice().forEach(function (idx) {
        if (!wanted[idx] && !channelCtrl.setChannelActive(idx, false)) applied = false;
      });
      chans.forEach(function (ch) {
        if (!channelCtrl.setChannelColor(ch.index, ch.color)) applied = false;
      });
      return { ok: true, applied: applied };
    }));
    // Stage 5-2：通用 SDK 插件经 bridge 创建测试标注。gate 后复用平台现有
    // /api/annotation POST 路径（rect 类型，payload 带 slide/x/y/side_px/label=text），
    // 成功后按现有模式刷新标注面板与索引并 return {ok:true,id}；失败 throw → host 回
    // ok:false, error={code:"annotation_create_failed",...}。
    host.onRequest("annotation.create", gate("annotation.create", function (p) {
      return createPluginAnnotation(p);
    }));
    host.onRequest("annotation.read", gate("annotation.read", function () {
      // 通用权限门演示方法（manifest 未声明 annotation:read 的插件会被稳定拒绝）。
      // 非特权插件不被允许批量读标注；已授权路径走 REST /api/annotations。
      throw { code: "permission_denied", message: "annotation.read 需经平台 REST 读取", retryable: false };
    }));
    // 标注卡点击聚焦：优先按 annotation_id 在 flatAnnoItems 里匹配 → 复用 jumpToAnno
    // （20% 边距 fitBounds + 选中蓝色描边 + focusAnno）。匹配不到但有几何 → 只定位
    // 视野并画临时 overlay 框（不动已有标注，不造假标注）。
    host.onRequest("annotation.focus", gate("annotation.focus", function (p) {
      p = p || {};
      var match = null;
      if (p.annotation_id) {
        var items = flatAnnoItems();
        for (var i = 0; i < items.length; i++) {
          if (String(items[i].annotation_id || "") === String(p.annotation_id)) { match = items[i]; break; }
        }
      }
      if (match) { jumpToAnno(match); return { ok: true, focused: true }; }
      // 升级 C：聚焦兜底优先成对 w/h；旧 side_px = 正方形兼容（不 max/min 冒充）
      var fw = Number(p.w) > 0 ? Number(p.w)
        : (Number(p.width_px) > 0 ? Number(p.width_px)
          : (Number(p.side_px) > 0 ? Number(p.side_px) : 0));
      var fh = Number(p.h) > 0 ? Number(p.h)
        : (Number(p.height_px) > 0 ? Number(p.height_px)
          : (Number(p.side_px) > 0 ? Number(p.side_px) : 0));
      if (viewer && viewer.viewport && p.x != null && fw > 0 && fh > 0) {
        var padX = fw * 0.2, padY = fh * 0.2;
        try {
          viewer.viewport.fitBounds(
            viewer.viewport.imageToViewportRectangle(p.x - padX, p.y - padY, fw + padX * 2, fh + padY * 2));
        } catch (e) {}
        aiOverlay = [{ x: p.x, y: p.y, w: fw, h: fh, magnification: "" }];
        redrawAnnoCanvas();
      }
      return { ok: true, focused: false };
    }));
    // Plugin→Host event
    host.onEvent("notification.show", function (p) {
      toast(p && p.msg, (p && p.type) || "info");
    });
    host.onEvent("annotation.changed", function () {
      // 插件落 AI 标注后 → 刷新当前切片标注面板与索引
      refreshCurrentAnnotations();
      loadAnnotationsIndex();
    });
    host.onEvent("panel.stateChanged", function (p) {
      // 插件打开 AI 面板时关闭标注面板（保留原 openAiPanel/openAiPanel 的互斥语义）
      if (p && p.open && annoPanelOpen) closeAnnoPanel();
    });
  }

  // 通用插件 annotation.create 的 host 端实现（Stage 5-2 / 升级 C）。入参
  // p: {text, x, y, w, h}（level-0 坐标，v2 成对 w/h 直通）或旧形态
  // {text, x, y, side_px}（正方形兼容）。**不再取 max(w,h) 正方形化**。
  // 复用 app.js 现有创建标注的 fetch 形态（见 saveAnnotation，POST /api/annotation，
  // body 含 slide/type/label + 几何字段），成功触发 refreshCurrentAnnotations +
  // loadAnnotationsIndex（与 saveAnnotation / 插件 annotation.changed 一致），
  // 返回 {ok:true, id}。失败 throw {code:"annotation_create_failed", message}。
  function createPluginAnnotation(p) {
    if (!state.slide) throw { code: "annotation_create_failed", message: "当前无切片", retryable: false };
    p = p || {};
    // P2 合同 §3.1：slide_id（权威）+ slide（名称快照）双字段并存
    var body = {
      slide: state.slide.name,
      type: "rect",
      label: String(p.text != null ? p.text : "插件标注"),
      x: Math.round(Number(p.x) || 0),
      y: Math.round(Number(p.y) || 0),
    };
    if (state.slide.id) body.slide_id = state.slide.id;
    var pw = Number(p.w), ph = Number(p.h), ps = Number(p.side_px);
    if (isFinite(pw) && pw > 0 && isFinite(ph) && ph > 0) {
      // v2：w/h 直通（插件的矩形不再被取 max 转正方形）
      body.w = Math.round(pw);
      body.h = Math.round(ph);
      // v2 与 side_px 同给：仅一致正方形的冗余兼容，矛盾组合拒绝
      if (isFinite(ps) && ps > 0 && !(Math.round(ps) === body.w && Math.round(ps) === body.h)) {
        throw { code: "annotation_create_failed", message: "side_px 与 w/h 冲突", retryable: false };
      }
    } else if (isFinite(ps) && ps > 0) {
      body.side_px = Math.round(ps); // 旧调用形态：正方形兼容
    } else {
      throw { code: "annotation_create_failed", message: "标注尺寸非法", retryable: false };
    }
    return apiFetch("/api/annotation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (!r.ok) {
        return r.json().then(function (j) {
          throw { code: "annotation_create_failed", message: (j && j.error) || "标注创建失败", retryable: false };
        });
      }
      return r.json();
    }).then(function (res) {
      refreshCurrentAnnotations();
      loadAnnotationsIndex().then(function () {
        if (typeof renderFolderBrowser === "function") renderFolderBrowser();
      }).catch(function () {});
      return { ok: true, id: res && (res.id || res.index) };
    });
  }

  // 当前选区 bbox（ROI 矩形 或 选中标注），供插件 selection.getBbox 使用。
  // 升级 C：level-0 {x,y,w,h}；不再以单边长冒充 w/h。
  function currentSelectionBbox() {
    if (state.roi && state.roi.w > 0 && state.roi.h > 0) {
      return { x: state.roi.x, y: state.roi.y, w: state.roi.w, h: state.roi.h };
    }
    if (editItem && editItem.type === "rect" && rectItemW(editItem) > 0) {
      return { x: editItem.x, y: editItem.y,
               w: rectItemW(editItem), h: rectItemH(editItem) };
    }
    return null;
  }

  // E3（viewer-drawing-skill-contract §2.1）：viewer.getSkillTarget 的单一实现。
  // 技能目标冻结的宿主侧取值（发送时由插件经桥调用一次；本函数只读，无副作用）：
  //   - kind="annotation"：当前选中标注（稳定 annotation_id + revision + level-0
  //     包围盒）。结果永远是新建 pending 标记——本查询不授予编辑该标注的权限；
  //   - kind="roi"：选框 ROI（level-0，取整）；
  //   - kind="viewport"：当前视野 level-0 bbox（level0ViewportBbox 单一实现）；
  //   - slide_id / asset_revision 随当前切片身份下发（宽容缺省 null——旧后端
  //     DTO 无 slide_id / asset_revision 时，切片一致性由 PT /api/ai/run 的
  //     slide ref 校验兜底）；
  //   - 无切片 → {available:false, reason:"no_slide"}；临时查看切片（§5.5 只读
  //     视图）→ {available:false, reason:"readonly"}——都不抛错，插件按「暂
  //     不可用」说明呈现。
  function currentSkillTarget() {
    if (!state.slide) return { available: false, reason: "no_slide" };
    if (state.slide.temporaryViewExpiresAt) return { available: false, reason: "readonly" };
    var slideId = state.slide.id || null;
    var assetRevision = state.slide.revision || null;
    // 1) 选中标注优先（须有稳定 annotation_id；几何取三形包围盒，非法回落）。
    if (editItem && editItem.annotation_id) {
      var ab = annoItemBbox(editItem);
      if (ab) {
        return {
          available: true, kind: "annotation", bbox: ab,
          slide_id: slideId, asset_revision: assetRevision,
          annotation_id: String(editItem.annotation_id),
          annotation_revision: Number(editItem.revision) > 0 ? Math.round(Number(editItem.revision)) : null,
        };
      }
    }
    // 2) 选框 ROI。
    if (state.roi && state.roi.w > 0 && state.roi.h > 0) {
      return {
        available: true, kind: "roi",
        bbox: {
          x: Math.round(Number(state.roi.x) || 0),
          y: Math.round(Number(state.roi.y) || 0),
          w: Math.round(Number(state.roi.w) || 0),
          h: Math.round(Number(state.roi.h) || 0),
        },
        slide_id: slideId, asset_revision: assetRevision,
        annotation_id: null, annotation_revision: null,
      };
    }
    // 3) 当前视野（viewer 未就绪/几何非法 → available:false，不抛错）。
    var vb = level0ViewportBbox();
    if (!vb) return { available: false, reason: "viewer_not_ready" };
    return {
      available: true, kind: "viewport", bbox: vb,
      slide_id: slideId, asset_revision: assetRevision,
      annotation_id: null, annotation_revision: null,
    };
  }

  // 标注面板行内的 AI 动作（AI 入口统一 2026-10-18）：
  // 独立的 fork「快速问答」与 branch「从此处深读」入口已退役——不再从标注
  // 行隐式新建批注小对话/分支会话；行内只保留「✨ 加入 AI 对话」一个入口，
  // 与浮条/右键菜单同一冻结实现（attachMarkerToConversation）。历史 fork/
  // branch 会话仍可在 AI 面板查看与续聊（插件侧兼容适配，有期限）。
  function buildAnnoAiActions(container, annotationId, style) {
    if (!annotationId || !container) return;
    var it = null;
    var items = flatAnnoItems();
    for (var i = 0; i < items.length; i++) {
      if (String(items[i].annotation_id || "") === String(annotationId)) { it = items[i]; break; }
    }
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = (style === "op") ? "ai-op ai-attach-anno" : "ai-action-chip ai-attach-anno";
    btn.title = tt("anno.menu.askai.tip");
    btn.innerHTML = '<span class="ai-act-ic">✨</span><span class="ai-act-tx">' +
      esc(tt("anno.menu.askai")) + "</span>";
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      attachMarkerToConversation(it || { annotation_id: annotationId });
    });
    if (!it) { btn.disabled = true; btn.title = tt("anno.menu.askai.unavailable"); }
    container.appendChild(btn);
  }

  // =========================================================================
  // 工单 E（plan §6）：查看器右键菜单 —— 把当前视野 / 标注加入 AI 会话草稿
  // -------------------------------------------------------------------------
  // - 查看器 overlay 右键：preventDefault（不弹浏览器菜单、不启动绘制）；
  //   非绘制进行中时弹平台上下文菜单。菜单**打开时冻结**载荷（level-0 bbox
  //   权威、视野中心与右键点位分列、倍率、render context、marker 元数据），
  //   点击菜单项才经 HostBridge emit `conversation.attachIntent` 给插件——
  //   只进会话草稿，不自动发送、不启动 AI、不开分支。
  // - 插件未加载：toast「AI 插件未加载」，不假装已加入。
  // - 绘制进行中（箭头/描图/矩形拖出）：右键仅 preventDefault 不弹菜单，
  //   绘制取消逻辑归绘制工单（D），不在此抢交互。
  // =========================================================================

  // 当前视野 level-0 像素 bbox（viewer.getViewport 桥方法与右键菜单共用的
  // 单一实现）：getBounds(true) → viewportToImageRectangle → 钳到切片边界
  // [0,0,width,height] → 取整。viewer 未就绪 / 几何非法 → null（调用方决定
  // 错误语义）。
  function level0ViewportBbox() {
    if (!state.slide || !viewer || !viewer.viewport) return null;
    try {
      var bounds = viewer.viewport.getBounds(true);
      var rect = viewer.viewport.viewportToImageRectangle(bounds);
      var vx = Number(rect && rect.x), vy = Number(rect && rect.y);
      var vw = Number(rect && rect.width), vh = Number(rect && rect.height);
      if (!isFinite(vx) || !isFinite(vy) || !isFinite(vw) || !isFinite(vh)) return null;
      var sw = Number(state.slide.width) || 0, sh = Number(state.slide.height) || 0;
      var x0 = Math.min(Math.max(vx, 0), sw);
      var y0 = Math.min(Math.max(vy, 0), sh);
      var x1 = Math.min(Math.max(vx + vw, 0), sw);
      var y1 = Math.min(Math.max(vy + vh, 0), sh);
      var rx0 = Math.round(x0), ry0 = Math.round(y0);
      return {
        x: rx0,
        y: ry0,
        w: Math.max(0, Math.round(x1) - rx0),
        h: Math.max(0, Math.round(y1) - ry0),
      };
    } catch (e) { return null; }
  }

  // 当前倍率文案（zoomText 同源："20×" / "35%"）；不可得 → null
  function currentViewerMagnification() {
    try {
      if (window.HP_ViewerCore && HP_ViewerCore.zoomText) {
        var s = HP_ViewerCore.zoomText(viewer, state.mppX);
        return (s && s !== "—") ? s : null;
      }
    } catch (e) {}
    return null;
  }

  // 当前查看器 render context（Batch 4 桥接口径：window.PathTogether.renderState；
  // RGB/未启用 → null）——快照冻结用，不含短期令牌。
  function viewerRenderContextSnapshot() {
    try {
      var rs = window.PathTogether && window.PathTogether.renderState;
      return (rs && rs.renderContext) || null;
    } catch (e) { return null; }
  }

  // 标注几何 → level-0 包围盒（rect/arrow/freehand/polygon 四形；无效 → null）
  function annoItemBbox(it) {
    if (!it) return null;
    var typ = it.type || "rect";
    var x, y, w, h;
    if (typ === "arrow") {
      x = Math.min(Number(it.x1), Number(it.x2)); y = Math.min(Number(it.y1), Number(it.y2));
      w = Math.abs(Number(it.x2) - Number(it.x1)); h = Math.abs(Number(it.y2) - Number(it.y1));
    } else if ((typ === "freehand" || typ === "polygon") && it.points && it.points.length) {
      var xs = it.points.map(function (p) { return Number(p[0]); });
      var ys = it.points.map(function (p) { return Number(p[1]); });
      x = Math.min.apply(null, xs); y = Math.min.apply(null, ys);
      w = Math.max.apply(null, xs) - x; h = Math.max.apply(null, ys) - y;
    } else {
      x = Number(it.x); y = Number(it.y);
      w = Number(rectItemW(it)); h = Number(rectItemH(it));
    }
    if (!isFinite(x) || !isFinite(y) || !isFinite(w) || !isFinite(h) || w <= 0 || h <= 0) return null;
    return { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) };
  }

  var ctxMenuEl = null;       // 惰性创建的菜单 DOM（#viewer-ctx-menu）
  var ctxMenuFrozen = null;   // 菜单打开时冻结的载荷 {viewport: payload|null, marker: payload|null}
  var ctxMenuCloser = null;   // 打开期间挂的 document 级关闭监听清理函数

  function ensureViewerCtxMenu() {
    if (ctxMenuEl && ctxMenuEl.parentNode) return ctxMenuEl;
    var m = document.createElement("div");
    m.id = "viewer-ctx-menu";
    m.className = "viewer-ctx-menu";
    m.setAttribute("role", "menu");
    m.style.display = "none";
    // 菜单自身右键：吞掉，不再叠一层浏览器默认菜单
    m.addEventListener("contextmenu", function (e) {
      e.preventDefault(); e.stopPropagation();
    });
    document.body.appendChild(m);
    ctxMenuEl = m;
    return m;
  }

  function closeViewerCtxMenu() {
    if (ctxMenuCloser) { try { ctxMenuCloser(); } catch (e) {} ctxMenuCloser = null; }
    ctxMenuFrozen = null;
    if (ctxMenuEl) ctxMenuEl.style.display = "none";
  }

  // 菜单项点击 → emit attachIntent（仅加入草稿；插件未加载明确提示，不假装成功）
  function emitConversationAttachIntent(payload) {
    if (!payload) return;
    if (!hpReady()) {
      toast(t("ctxmenu.attach.plugin.missing"), "error");
      return;
    }
    hpEmit("conversation.attachIntent", payload);
    // 加入成功提示按附件类型区分（roi = 未保存草稿区域，无标注语义）
    var addedKey = payload.kind === "marker" ? "ctxmenu.attach.anno.added"
      : (payload.kind === "roi" ? "ctxmenu.attach.roi.added"
        : "ctxmenu.attach.view.added");
    toast(t(addedKey), "success");
  }

  // ---------- 载荷冻结（菜单打开时执行；字段与工单 E 契约一一对应） ----------
  // clickPt 为右键图像点位（不可得 → null）；**绝不**把点位冒充视野中心：
  // center 一律由冻结 bbox 推导，与 click_point 分列。
  function freezeViewportAttachPayload(clickPt) {
    var bbox = level0ViewportBbox();
    if (!bbox) return null;
    return {
      kind: "viewport",
      // P2 合同 §5：slide_id 操作键 + slide 名称快照并存（插件侧按 id 归档，
      // 旧插件按 name——退役条件 P6）
      slide_id: (state.slide && state.slide.id) || null,
      slide: state.slide.name,
      bbox: bbox,
      click_point: clickPt || null,
      center: { x: Math.round(bbox.x + bbox.w / 2), y: Math.round(bbox.y + bbox.h / 2) },
      magnification: currentViewerMagnification(),
      render_context: viewerRenderContextSnapshot(),
      annotation_id: null, revision: null, type: null, geometry: null, note: null,
      frozen_at: Math.floor(Date.now() / 1000),
    };
  }

  function freezeMarkerAttachPayload(it, clickPt) {
    if (!it) return null;
    var bbox = annoItemBbox(it);
    if (!bbox) return null;
    var geom = null;
    try { geom = snapshotGeom(it); } catch (e) { geom = null; }
    return {
      kind: "marker",
      slide_id: (state.slide && state.slide.id) || null,
      slide: state.slide.name,
      bbox: bbox,   // 标注包围盒（发送时作为冻结 viewport，不重查实时视野）
      click_point: clickPt || null,
      center: { x: Math.round(bbox.x + bbox.w / 2), y: Math.round(bbox.y + bbox.h / 2) },
      magnification: currentViewerMagnification(),
      render_context: viewerRenderContextSnapshot(),
      annotation_id: (it.annotation_id != null ? String(it.annotation_id) : null),
      revision: (it.revision != null ? Number(it.revision) : null),
      type: it.type || "rect",
      // 标记名（用户署名）：仅展示用；模型上下文里的 label 以服务端
      // _validate_ai_attachments 解析授权后写入的权威值为准，此处值不采信。
      label: (it.label != null ? String(it.label) : null),
      geometry: geom,
      note: (it.note != null ? String(it.note) : null),   // 未受信文本：仅透传展示
      frozen_at: Math.floor(Date.now() / 1000),
    };
  }

  // 未保存候选框 → ROI 附件载荷（ROI 附件 2026-10-18）。点击「✨ 加入 AI
  // 对话」的时刻冻结：level-0 bbox 取自当前草稿 state.roi（取整、必须为正），
  // 切片身份（slide_id + name 快照）、倍率与 render context 同一刻取值。
  // 绝不伪造 annotation_id/revision，也不悄悄落库——发送后由 PT 网关按
  // kind=roi 白名单重建（只认 bbox/render_context，不查任何标注）。
  // 此后草稿继续移动/缩放、或保存成标记，都不影响已冻结的附件载荷
  // （冻结的是独立对象；保存后的标记要加入对话走 marker 入口重新冻结）。
  // 无有效草稿（非矩形工具 / 宽高 ≤ 0 / 数值非法）→ null，调用方给明确提示。
  function freezeDraftAttachPayload() {
    if (!rectToolActive() || !(state.roi.w > 0) || !(state.roi.h > 0)) return null;
    var x = Math.round(Number(state.roi.x)), y = Math.round(Number(state.roi.y));
    var w = Math.round(Number(state.roi.w)), h = Math.round(Number(state.roi.h));
    if (!isFinite(x) || !isFinite(y) || !isFinite(w) || !isFinite(h) ||
        w <= 0 || h <= 0) return null;
    var bbox = { x: x, y: y, w: w, h: h };
    return {
      kind: "roi",
      slide_id: (state.slide && state.slide.id) || null,
      slide: state.slide.name,
      bbox: bbox,
      click_point: null,
      center: { x: Math.round(bbox.x + bbox.w / 2), y: Math.round(bbox.y + bbox.h / 2) },
      magnification: currentViewerMagnification(),
      render_context: viewerRenderContextSnapshot(),
      annotation_id: null, revision: null, type: null,
      geometry: null, note: null, label: null,
      frozen_at: Math.floor(Date.now() / 1000),
    };
  }

  // ---------- 打开 / 事件入口 ----------
  function openViewerContextMenu(e) {
    if (!state.slide) return;
    var m = ensureViewerCtxMenu();
    closeViewerCtxMenu();
    // 冻结（菜单打开时刻）：右键点位（图像坐标，可得则记）+ 视野 + 命中/选中标注
    var clickPt = null;
    try { clickPt = screenToImg(e); } catch (err) { clickPt = null; }
    var frozen = { viewport: freezeViewportAttachPayload(clickPt), marker: null };
    var markerIt = null;
    if (state.showAnno && !state.drawMode) {
      try { markerIt = hitAnno(screenPt(e).x, screenPt(e).y); } catch (err) { markerIt = null; }
    }
    if (!markerIt) markerIt = editItem || state.focusAnno;   // 显式选中兜底
    if (markerIt) frozen.marker = freezeMarkerAttachPayload(markerIt, clickPt);
    ctxMenuFrozen = frozen;

    // 菜单项构建：视野项（冻结失败不显示死项）；标注项（无命中且无选中不显示）
    m.innerHTML = "";
    var appendItem = function (storeKey, label) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "viewer-ctx-menu-item";
      b.setAttribute("role", "menuitem");
      b.dataset.attachKind = storeKey;
      b.textContent = label;
      b.addEventListener("click", function (ev) {
        ev.preventDefault(); ev.stopPropagation();
        var payload = (ctxMenuFrozen && ctxMenuFrozen[storeKey]) || null;
        closeViewerCtxMenu();
        emitConversationAttachIntent(payload);
      });
      m.appendChild(b);
    };
    if (frozen.viewport) appendItem("viewport", t("ctxmenu.attach.view"));
    if (frozen.marker) appendItem("marker", t("ctxmenu.attach.anno"));
    if (!m.children.length) return;   // 无可附内容（如 viewer 未就绪）：不开菜单

    // 定位：右键点附近；越界按窗口尺寸钳回
    var x = e.clientX || 0, y = e.clientY || 0;
    m.style.display = "block";
    try {
      var vw0 = window.innerWidth || 0, vh0 = window.innerHeight || 0;
      var mw = m.offsetWidth || 180, mh = m.offsetHeight || 60;
      if (vw0 && x + mw > vw0 - 8) x = Math.max(8, vw0 - mw - 8);
      if (vh0 && y + mh > vh0 - 8) y = Math.max(8, vh0 - mh - 8);
    } catch (err) { /* 无布局环境（测试）：原样定位 */ }
    m.style.left = x + "px";
    m.style.top = y + "px";

    // 关闭：外部 pointerdown/click、Escape、另处右键、窗口 resize
    var onDocClose = function (ev) {
      try {
        if (ev && ev.target === m) return;   // 菜单内事件不关（click 由菜单项自理）
      } catch (err) {}
      closeViewerCtxMenu();
    };
    var onKey = function (ev) {
      if (ev && ev.key === "Escape") closeViewerCtxMenu();
    };
    document.addEventListener("pointerdown", onDocClose, true);
    document.addEventListener("click", onDocClose, true);
    document.addEventListener("contextmenu", onDocClose, true);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", closeViewerCtxMenu);
    ctxMenuCloser = function () {
      document.removeEventListener("pointerdown", onDocClose, true);
      document.removeEventListener("click", onDocClose, true);
      document.removeEventListener("contextmenu", onDocClose, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", closeViewerCtxMenu);
    };
  }

  // contextmenu 入口（绑定在标注画布层）：绘制进行中只吞默认菜单（取消逻辑
  // 归工单 D），否则弹附件菜单。
  function onViewerContextMenu(e) {
    e.preventDefault();
    e.stopPropagation();
    if (!state.slide) return;
    if ((state.drawMode && drawPreview) || (rectToolActive() && rectDrawInfo)) return;
    openViewerContextMenu(e);
  }

  // ---------- 启动 ----------
  // 装配侧栏开合控制器（升级 A）：真实 DOM/媒体查询/storage/userScope 注入
  function initSidebarController() {
    var mq = window.matchMedia ? window.matchMedia(SB_MOBILE_QUERY) : null;
    sidebarCtrl = createSidebarController({
      sidebar: els.sidebar,
      sidebarMask: els.sidebarMask,
      menuBtn: els.menuBtn,
      mq: mq,
      storage: safeLocalStorage(),
      scope: userScope,
      doc: document,
      onLayoutChange: syncViewerLayoutAfterSidebar,
      focusSearch: function () {
        // 改版 2026-10-08：搜索入口移至顶栏「搜索」浮层——「选择切片」展开
        // 侧栏后焦点落在顶栏搜索按钮
        if (els.tbSearchBtn && typeof els.tbSearchBtn.focus === "function") {
          try { els.tbSearchBtn.focus(); } catch (e) { /* 忽略聚焦失败 */ }
        }
      },
    });
    sidebarCtrl.init();
    // 断点切换：清理手机遮罩、恢复当前设备布局状态（§4.1 末条）
    var mq = window.matchMedia ? window.matchMedia(SB_MOBILE_QUERY) : null;
    if (mq) {
      var onMqChange = function () { sidebarCtrl.onBreakpointChange(); };
      if (typeof mq.addEventListener === "function") mq.addEventListener("change", onMqChange);
      else if (typeof mq.addListener === "function") mq.addListener(onMqChange);
    }
  }

  // viewer 画质档（image-transport-upgrade §3.3/§5.2）：三入口共用
  // HP_ViewerEncoding；本页只注入宿主/文案与重开回调。画质切换走
  // channelCtrl.reopenForQuality（轻量路径：不改 context、不重绑 AI）。
  function initQualityControl() {
    if (!window.HP_ViewerEncoding) return;
    HP_ViewerEncoding.mount({
      host: els.qualityControl,
      t: t,
      toast: function (msg, type) { toast(msg, type); },
      onQualityReopen: function () {
        if (channelCtrl) channelCtrl.reopenForQuality();
      },
    });
    HP_ViewerEncoding.installConflictRecovery({
      viewer: viewer,
      onConflict: function () {
        // 409 display_version_conflict：只刷新 info 并重建一次（保留选择/视口）
        if (channelCtrl) channelCtrl.recoverDisplayConflict();
      },
    });
  }

  function init() {
    initViewer();
    initQualityControl();
    initSidebarController();
    // AI 面板外框拖动/调整大小（改版第四轮 §1.4；demo 面板无头部 id → 不启用）
    initAIPanelController();
    bindEvents();
    // 抽出层跟随窗口尺寸/滚动失效（改版第四轮 §1.1；捕获式滚动含侧栏内滚）
    document.addEventListener("pointermove", function (e) {
      if (fbFanDismissedAt && (Math.abs(e.clientX - fbFanDismissedAt.x) >= 2 || Math.abs(e.clientY - fbFanDismissedAt.y) >= 2)) fbFanDismissedAt = null;
      fbFanMove(e);
    }, true);
    document.addEventListener("pointerdown", function () {
      // Do not insert a portal between pointerdown and pointerup: the browser
      // would dispatch click to their common ancestor (body), losing the row.
      if (fbPulloutShowTimer) { clearTimeout(fbPulloutShowTimer); fbPulloutShowTimer = null; }
    }, true);
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") { fbPulloutHide(); cancelSlideDeal(); } });
    window.addEventListener("blur", fbPulloutHide);
    window.addEventListener("resize", function () { fbPulloutOnLayoutChange(); cancelSlideDeal(); });
    window.addEventListener("blur", cancelSlideDeal);
    document.addEventListener("scroll", function () {
      // A pending hover measures its anchor after scrolling; only an already
      // placed preview has stale coordinates. Do not eat the first hover.
      if (fbPulloutEl) fbPulloutHide();
    }, true);
    // §3.3 宽度断点分组：先于首次布局执行（把折叠档的节点搬入 ⋯ 菜单）
    initToolbarTier();
    setupDragDrop();
    // COS 直传 Phase 4：capability 可用时渲染手动开关，并恢复未完任务的
    // 只读进度行（off 时均为无操作，上传管线行为不变）
    initCosUploadUi();
    // P2 问题 3：上传行容器在抽屉/侧栏之间的跟随（块显隐的 MutationObserver）
    initUploadRowHostSync();
    // 上传面板内转换（设计 §6.2 第 1/2 步）：启动期**只列出**带
    // `intent.channel === 'workbench-inline'` 的未完成任务 → 行 + 「继续转换」
    // /「继续上传」/「删除本地产物」；绝不自动开始转换或上传。
    // 必须在这里显式调用：restore() 只挂在 ensureInlineOrch() 里，而此前
    // **没有任何启动期入口**调它——刷新后恢复行永不出现，§6.3 唯一的 OPFS
    // 清理入口随之不可达（`_sweepIncompleteStaging` 显式跳过带 upload 记录的
    // ready/published 任务，不兜底 → 源副本 + 产物永久残留）。开关关闭时不
    // 加载运行器（回退路径与「加载期零面板请求」语义均不变）。
    if (inlineEnabled()) {
      ensureInlineOrch();
    } else if (/[?&]save-job=/.test(window.location.search || "")) {
      // 复核 r2 #2：移交标记无法兑现（面板内上传未开启）→ 显式说明，
      // 绝不静默忽略；标记参数照常摘除
      showSaveHandoffNotice(
        tt("upload.saveproj.fail.prefix") +
        tt("upload.saveproj.reason.capabilityOff"),
        [{ label: tt("upload.saveproj.notice.dismiss"), onClick: null }]);
      try {
        var u = new URL(window.location.href);
        u.searchParams.delete("save-job");
        window.history.replaceState({}, "", u.pathname + u.search + u.hash);
      } catch (e) { /* 摘不掉不致命 */ }
    }
    initAuth();
    // P3 研究采集装配（capabilities.research_collection 开启才工作）
    initResearchTelemetry();
    // 注册 HistoPilot HostBridge host 能力（插件未启用时为空操作）
    registerHostBridgeHandlers();
    // 改版 2026-10-08：旧「未归类/分享管理」折叠区已由文件夹浏览器与顶栏
    // 分享浮层替代（无初始折叠区）。
    // 升级 A：启动时无切片，显示空态入口（openSlide 成功后隐藏）
    updateViewerEmptyState();
    updateCanvasSlideLabel();
    var listLoaded = loadAll();
    // P2 合同 §5.1：?slide=<slide_id> URL 通道——加载时若带此参数打开对应切片
    // （与 /s/<token>、/login?next= 无语义冲突；旧链接无此参数不受影响）。
    // 旧后端（slide_id_api=false）该参数按名回落打开。
    openSlideFromLocation(listLoaded);
  }

  function openSlideFromLocation(listLoaded) {
    var m = null;
    try { m = location.search.match(/[?&]slide=([^&]+)/); } catch (e) { return; }
    if (!m) return;
    var ref = decodeURIComponent(m[1]);
    if (!ref) return;
    openSlide(ref);
    // 侧栏翻到深链切片所在的文件夹与叠（与搜索定位同一函数）
    if (listLoaded && listLoaded.then) {
      listLoaded.then(function () {
        for (var i = 0; i < allSlides.length; i++) {
          var s = allSlides[i];
          if (slideRefOf(s) === ref || s.name === ref) { fbLocateSlide(s); return; }
        }
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // 供测试（tests/js/project-import-upgrade.test.ts）：__PT_TEST_HOOKS 由测试
  // 预置后再加载 app.js 才挂载；生产环境不暴露任何额外全局（与 HP_AUTH/
  // HP_UPLOAD 同风格）。置于 IIFE 末尾：确保引用的状态对象已初始化。
  if (window.__PT_TEST_HOOKS) {
    window.HP_PROJECT_UI = {
      projectDialog: projectDialog,
      openProjectDialog: openProjectDialog,
      closeProjectDialog: closeProjectDialog,
      submitProjectDialog: submitProjectDialog,
      submitFormatRequest: submitFormatRequest,
      importDrawer: {
        state: importDrawerState,
        targetState: importTargetState,
        open: openImportDrawer,
        close: closeImportDrawer,
        switchTab: switchImportTab,
        refreshTasks: refreshImportTasks,
        loadFormatCatalog: loadImportFormatCatalog,
        associate: importAssociateUploaded,
        // folder-experience §1：上传开始冻结目标的测试驱动面（真实入口
        // uploadFile 内同一函数；仅测试预置 __PT_TEST_HOOKS 才挂载）
        snapshot: importTargetSnapshot,
        defaultTargetPid: importDefaultTargetPid,
      },
      baidu: {
        state: baiduState,
        refreshCapabilities: refreshBaiduCapabilities,
        startEnumeration: startBaiduEnumeration,
        loadCandidates: loadBaiduCandidates,
        toggleCandidate: toggleBaiduCandidate,
        startImport: startBaiduImport,
        renderImport: renderBaiduImport,
        formatDecBytes: formatDecBytes,
      },
      // Wave 3（普通图片兼容）测试入口：accept 派生/无物理标尺单位区/像素标注保存。
      // 与 HP_UPLOAD 同约定：仅当测试预置 __PT_TEST_HOOKS 才挂载，生产不暴露。
      formats: {
        fallbackCatalog: FORMAT_CATALOG_FALLBACK,
        acceptFallback: FILE_INPUT_ACCEPT_FALLBACK,
        acceptFromCatalog: acceptFromCatalog,
        applyFileInputAccept: applyFileInputAccept,
      },
      viewerState: state,
      openSlide: openSlide,
      // 改版 2026-10-08（§5）：文件夹浏览器/搜索/临时查看的测试驱动面
      // （与 openSlide 同约定：仅当测试预置 __PT_TEST_HOOKS 才挂载）
      fb: {
        state: fbState,
        FB_TEMP_KEY: FB_TEMP_KEY,
        pageSize: fbPageSize,
        bandGap: fbBandGap,
        entries: fbEntries,
        pathOf: fbPathOf,
        search: fbSearchSlides,
        locationText: fbSlideLocationText,
        render: renderFolderBrowser,
        go: fbGo,
        locate: fbLocateSlide,
        reload: reloadProjectsAndUnfiled,
        endTemporaryView: endTemporaryView,
        armTempViewTimer: armTempViewTimer,
        updateCanvasLabel: updateCanvasSlideLabel,
        // 改版第四轮 §1.1：抽出层测试驱动面（层必须在 body，不在侧栏内）
        pullout: {
          show: fbPulloutShow,
          hide: fbPulloutHide,
          scheduleHide: fbPulloutScheduleHide,
          current: function () { return fbPulloutEl; },
          hit: function () { return fbPulloutHit; },
        },
      },
      share: {
        setPending: function (refs) { sharePendingSlides = refs; },
        pending: function () { return sharePendingSlides; },
        updateTargetLine: updateShareTargetLine,
      },
      slideHasPhysicalScale: slideHasPhysicalScale,
      syncUnitAvailability: syncUnitAvailability,
      setMpp: setMpp,
      saveAnno: saveAnno,
      slideMetaTags: slideMetaTags,
    };
  }

  // 语言切换：重渲染当前可见的动态面板（动态文本走 t()，重渲染即换语言）。
  // 静态 [data-i18n] 节点由 i18n.js 的 applyLang 直接刷新，这里只处理 JS 渲染的部分。
  document.addEventListener("hp-lang-change", function () {
    try {
      // 文件夹浏览器 / 分享列表（只要数据已加载就重渲）
      renderFolderBrowser();
      if (allSharesCache) renderShareList(allSharesCache);
    } catch (e) {}
    try {
      // 标注面板（打开时才重渲）
      if (annoPanelOpen && currentAnnotations) {
        renderAnnoPanel(currentAnnotations.annotations || []);
      }
    } catch (e) {}
    // 升级 A：侧栏按钮文案/aria 随状态（展开↔收起）变化，切语言后重写
    try { if (sidebarCtrl) sidebarCtrl.refreshButton(); } catch (e) {}
    // 顶栏搜索/分享浮层（改版 2026-10-08）：动态文案随语言刷新
    try { refreshSearchTexts(); } catch (e) {}
    try { updateShareTargetLine(); } catch (e) {}
    // §3.4：document.title 的产品名后缀随语言刷新（切片名不变；P2：主显示
    // display_name，旧 DTO 回落 name）
    try {
      if (state.slide && state.slide.name) {
        updateDocTitle(slideDisplayName(state.slide) || state.slide.name);
      }
    } catch (e) {}
    // 账户 popover 角色文案随语言刷新
    try {
      if (els.acctPopRole && currentRole) {
        els.acctPopRole.textContent = t(currentRole === "owner" ? "acct.role.owner" : "acct.role.user");
      }
    } catch (e) {}
    // W3/W4/W6：导入抽屉 / 新建项目对话框的动态文案随语言重渲
    try {
      if (projectDialog.open) renderProjectDialogSlides();
      if (els.pcdConfirm && !projectDialog.inFlight) {
        els.pcdConfirm.textContent = t("pcd.confirm");
      }
    } catch (e) {}
    try {
      if (importDrawerState.open) {
        if (importDrawerState.formatsLoaded) renderFormatCatalog(importDrawerState.formatCatalog);
        renderFrSampleMax();
        refreshImportTasks();
      }
    } catch (e) {}
    // AI 配置摘要 / 会话切换器的语言重渲由 HistoPilot 插件 bundle 自行监听 hp-lang-change 处理。
  });
})();
