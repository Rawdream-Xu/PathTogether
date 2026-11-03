// tools-slides.js — /tools/slides 本地切片工具页（C3；本地转换 + 本地保存）。
// 页面唯一职责：UI、文件授权、设置选择与结果展示；全部转换逻辑经
// C2 运行器（slide-transform/runner.js，页面唯一 API 面）。零第三方依赖。
//
// 隐私（2026-10-20 收敛）：本页只做本地转换与本地保存——除自托管静态代码
// 与引擎外不发任何网络请求；文件字节/文件名/哈希/缩略图不出浏览器；不创建
// 平台任务。上传统一在工作台上传面板完成，本页不再提供任何上传入口。
//
// 结构：默认视图 = 标题 + 一行本地处理说明 + 大拖放区/选择文件；识别后
// 只出现一张配置摘要（文件/类型/输出/画质 + 折叠「更多选项」）与「开始
// 转换」。file input 与 drop 都进入同一 prepareSource 流程；一次只接受一个
// 切片文件（多文件给明确提示）；F3：MRXS 完整包经「选择文件夹（MRXS）」
// （webkitdirectory）或目录 drop（事件内遍历构造相对路径成员行）进入同一
// prepareBundleSource → runner.prepareBundle（缺成员在复制前被类型化拒绝）。
// 扩展名只是提示，识别靠 engine.js 的文件头魔数表（已知不支持文件在完整
// 复制前被拒绝）。已兼容的文件（OME-TIFF 等）提示「无需转换；上传请前往
// 工作台」。画质（U3 编码档）与输出格式同样遵循「prepared 可改并落盘、
// 开始即锁定、重开以任务记录为准」的合同。
//
// 可访问性：progress 用 role=progressbar + aria-valuenow；状态文本走
// aria-live；对话框 <dialog showModal> 原生焦点圈 + 打开者焦点还原；
// 所有状态同时有文字，不只靠颜色。file input 视觉隐藏但保持键盘可达。
'use strict';

import { SlideToolsRunner } from './slide-transform/runner.js';
import * as E from './slide-transform/engine.js';
import { bundleRoute, bundleRows, collectEntryFiles, folderNameFromRelPath }
  from './tools-slides-bundle.js';

const CHANNEL_JSON_MAX_BYTES = 1 << 20; // 伴随文件读取上限 1 MiB（有界）

const $ = (id) => document.getElementById(id);
const els = {
  inputSection: $('input-section'),
  inputMessage: $('input-message'),
  companionSection: $('companion-section'),
  companionMessage: $('companion-message'),
  companionHint: $('companion-hint'),
  companionPickBtn: $('companion-pick-btn'),
  companionCancelBtn: $('companion-cancel-btn'),
  dropZone: $('drop-zone'),
  pickFileBtn: $('pick-file-btn'),
  fileInput: $('file-input'),
  pickFolderBtn: $('pick-folder-btn'),
  folderInput: $('folder-input'),
  channelSection: $('channel-section'),
  channelInput: $('channel-input'),
  stageSection: $('stage-section'),
  stageStatus: $('stage-status'),
  stageProgress: $('stage-progress'),
  stageBar: $('stage-bar'),
  stageBytes: $('stage-bytes'),
  prepareCancelBtn: $('prepare-cancel-btn'),
  summarySection: $('summary-section'),
  summaryGrid: $('summary-grid'),
  outputSummaryText: $('output-summary-text'),
  qualityConflict: $('quality-conflict'),
  qualityFieldset: $('quality-fieldset'),
  qualityMrsxNote: $('quality-mrxs-note'),
  qualityNdpiNote: $('quality-ndpi-note'),
  qualityVmsNote: $('quality-vms-note'),
  qualityRasterNote: $('quality-raster-note'),
  qualityBifNote: $('quality-bif-note'),
  qualityTmapNote: $('quality-tmap-note'),
  tmapCalibrationNote: $('tmap-calibration-note'),
  qualityJp2kNote: $('quality-jp2k-note'),
  qualityPreserveDesc: $('quality-preserve-desc'),
  qualityCompactDesc: $('quality-compact-desc'),
  qualityLocked: $('quality-locked'),
  moreOptions: $('more-options'),
  probeSection: $('probe-section'),
  probeGrid: $('probe-grid'),
  estimateSection: $('estimate-section'),
  estimateGrid: $('estimate-grid'),
  profileSection: $('profile-section'),
  profileSuggest: $('profile-suggest'),
  policySection: $('policy-section'),
  policyStrictWarn: $('policy-strict-warn'),
  formatSection: $('format-section'),
  formatFieldset: $('format-fieldset'),
  formatLocked: $('format-locked'),
  runSection: $('run-section'),
  runJob: $('run-job'),
  convertBtn: $('convert-btn'),
  cancelBtn: $('cancel-btn'),
  runStatus: $('run-status'),
  runProgress: $('run-progress'),
  runBar: $('run-bar'),
  runBytes: $('run-bytes'),
  resultSection: $('result-section'),
  resultGrid: $('result-grid'),
  saveBtn: $('save-btn'),
  saveProjectBtn: $('save-project-btn'),
  saveProjectDialog: $('save-project-dialog'),
  saveProjectForm: $('save-project-form'),
  saveProjectFile: $('save-project-file'),
  saveProjectStatus: $('save-project-status'),
  saveProjectFields: $('save-project-fields'),
  saveProjectAccount: $('save-project-account'),
  saveProjectDest: $('save-project-dest'),
  saveProjectNewRow: $('save-project-new-row'),
  saveProjectNewName: $('save-project-new-name'),
  saveProjectError: $('save-project-error'),
  saveProjectConfirmBtn: $('save-project-confirm-btn'),
  saveProjectCancelBtn: $('save-project-cancel-btn'),
  saveLoginForm: $('save-login-form'),
  saveLoginDialog: $('save-login-dialog'),
  saveLoginLoginBtn: $('save-login-login-btn'),
  saveLoginRegisterBtn: $('save-login-register-btn'),
  saveLoginCancelBtn: $('save-login-cancel-btn'),
  saveStatus: $('save-status'),
  saveCapabilityNote: $('save-capability-note'),
  jobsDetails: $('jobs-details'),
  jobsList: $('jobs-list'),
  pageError: $('page-error'),
  pageStatus: $('page-status'),
  diskDialog: $('disk-dialog'),
  diskTitle: $('disk-dialog-title'),
  diskBody: $('disk-dialog-body'),
  diskConfirm: $('disk-confirm-btn'),
  diskDeny: $('disk-deny-btn'),
};

const t = (key, vars) => window.HP_I18N.t(key, vars);

// ---------------------------------------------------------------- utils --

function fmtBytes(n) {
  if (!Number.isFinite(n)) return '—';
  if (n >= 2 ** 30) return `${(n / 2 ** 30).toFixed(2)} GiB`;
  if (n >= 2 ** 20) return `${(n / 2 ** 20).toFixed(1)} MiB`;
  if (n >= 2 ** 10) return `${(n / 2 ** 10).toFixed(1)} KiB`;
  return `${n} B`;
}

function errCode(e) {
  // stError 形状 {error:{code}} 与 worker done 的裸 {code,message} 都要认
  const c = E.errCode(e);
  return c || (e && typeof e.code === 'string' ? e.code : null);
}

function errRawText(e) {
  if (e && typeof e.code === 'string' && e.message) return `${e.code}: ${e.message}`;
  return E.errText(e);
}

function friendlyError(e) {
  const code = errCode(e);
  const raw = errRawText(e);
  const base = code ? t(`tools.err.${code}`) : null;
  // t() 回退返回 key 本身 —— 视为无翻译，退回原文
  const friendly = base && base !== `tools.err.${code}` ? base : null;
  if (code === 'disk_precheck_failed') return raw; // 磁盘缺口由对话框/数字呈现
  // 纯魔数拒绝（runner 的「不是本工具支持的 KFB/KFBF 文件」）交给通用
  // 友好文案，不再把只提 KFB/KFBF 的内部措辞呈现给用户（P2-8）。
  // 有 kind（tiff-sniff / raster-sniff / *-bundle）时保留具体原因。
  if (code === 'unsupported_input' && !(e && e.error && e.error.kind)) {
    return friendly || raw;
  }
  if (friendly) return `${friendly}\n(${raw})`;
  return raw;
}


function showError(e) {
  const text = friendlyError(e);
  els.pageError.hidden = false;
  els.pageError.textContent = text;
}

function showErrorText(text) {
  els.pageError.hidden = false;
  els.pageError.textContent = text;
}

function clearError() {
  els.pageError.hidden = true;
  els.pageError.textContent = '';
}

/// 带 i18n 键的动态文案（语言切换时按保存的键重渲染，避免残留旧语言）
function dynText(el, store, key, vars) {
  if (key === null) {
    store.key = null;
    el.textContent = '';
    return;
  }
  store.key = key;
  store.vars = vars || null;
  el.textContent = t(key, store.vars);
}
const stageMsg = { key: null, vars: null };
const statusMsg = { key: null, vars: null };
const inputMsg = { key: null, vars: null };
const companionMsg = { key: null, vars: null };       // 协助面板正文（随语言切换重渲染）
const companionPickMsg = { key: null, vars: null };   // 「选择「{stem}」文件夹」按钮
const companionHintMsg = { key: null, vars: null };   // 权限/不可用提示行
function setStageMsg(key, vars) { dynText(els.stageStatus, stageMsg, key, vars); }
function setPageMsg(key, vars) { dynText(els.pageStatus, statusMsg, key, vars); }
function showInputMessage(key, vars) { dynText(els.inputMessage, inputMsg, key, vars); }
function clearInputMessage() { dynText(els.inputMessage, inputMsg, null); }
function setCompanionMsg(key, vars) { dynText(els.companionMessage, companionMsg, key, vars); }
function setCompanionPick(key, vars) { dynText(els.companionPickBtn, companionPickMsg, key, vars); }
function setCompanionHint(key, vars) { dynText(els.companionHint, companionHintMsg, key, vars); }

/// 共享协助模块（classic script，全局惰性读取：classic defer 与 module 的
/// 执行顺序不保证，但所有用点都在用户手势/事件回调里，届时必然已就绪）
function companionModule() {
  return (typeof window !== 'undefined' && window.HP_MRXS_COMPANION) || null;
}

function setProgress(progressEl, barEl, bytesEl, frac, bytesText) {
  const pct = Math.max(0, Math.min(100, Math.round((frac || 0) * 100)));
  progressEl.setAttribute('aria-valuenow', String(pct));
  barEl.style.width = `${pct}%`;
  if (bytesEl) bytesEl.textContent = bytesText || '';
}

function dlRow(grid, term, definition, ddId) {
  const dt = document.createElement('dt');
  dt.textContent = term;
  const dd = document.createElement('dd');
  dd.textContent = definition;
  if (ddId) dd.id = ddId;
  grid.appendChild(dt);
  grid.appendChild(dd);
  return dd;
}

// ---------------------------------------------------------------- state --

const page = {
  runner: null,
  file: null,
  bundleFiles: null,       // F3：MRXS 完整包成员行 [{name, relPath, file}] | null
  bundleFolderName: null,  // F3：选择的文件夹名（任务列表显示用）
  channelJson: null,       // string | null（≤1 MiB 读取结果）
  channelJsonName: null,
  prep: null,              // { jobId, probe, identity }
  running: false,          // 转换正在执行（含从任务列表开始/续跑）
  busy: false,             // 页面级忙态：准备开始 → 终态（含转换）
  readyInfo: null,        // { jobId, outputBytes, sha256, modality, sourceName }
  outputLockedProfile: null,  // 明场输出格式：任务一旦开始即锁定（lockOutputProfile）
  encodingLockedProfile: null, // 画质：任务一旦开始即锁定（lockEncodingProfile）
  saveSupported: typeof window.showSaveFilePicker === 'function',
  beforeunloadOn: false,
  lastJobs: [],
  saveMsg: null,           // {key, vars} 保存状态文案（新输入/新结果时清空）
  missingArtifacts: new Set(), // 浏览器已清理产物的任务 id（不再提供「保存」）
  directHint: false,       // 当前输入是「无需转换」提示（语言切换需重渲染）
  prepareContext: null,    // { kind: 'file' | 'folder' | 'bundle', name }
  companion: null,         // MRXS 协助：{ stem, entryName, file, handle } ——
                           // 用户补选同名文件夹之前不复制任何内容
  runJobName: null,        // 转换卡上正在/刚完成的任务名（folderName||name）
  runJobState: null,       // 'running' | 'done' | 'failed' | 'cancelled' | null
};

/// worker/runner 的取消形态：旧 `{cancelled:true}`、新 `{type:'cancelled'}`
/// 与取消阶段都要按「已取消」而不是失败呈现（P2-5）。
function isCancelledResult(result) {
  return !!result
    && (result.type === 'cancelled' || result.cancelled === true
      || result.phase === 'cancelled');
}

const ARCHIVE_RE = /\.(zip|rar|7z|tar|gz|tgz|bz2|xz)$/i;

/// 新的输入尝试：清掉上一个任务的错误、状态与保存提示（P2-9：被拒绝的
/// 输入同样从干净状态开始；P1-3：新任务绝不继承上一个任务的「已保存」）。
function beginInputAttempt() {
  clearError();
  setPageMsg(null);
  clearInputMessage();
  page.saveMsg = null;
  page.directHint = false;
  clearCompanion();   // MRXS 协助是「待补选」状态：任何新输入尝试都作废旧提示
}

/// 准备失败的人类文案（P2-8）：MRXS / VMS 缺成员、文件夹里没有完整包、
/// 压缩包等各自成句；其余交给 friendlyError。
function prepareErrorMessage(e) {
  const code = errCode(e);
  const info = (e && e.error) || {};
  const ctx = page.prepareContext || {};
  if (code === 'unsupported_input'
      && (info.kind === 'mrxs-bundle' || info.kind === 'vms-bundle')) {
    const isMrxs = info.kind === 'mrxs-bundle';
    const placeholder = isMrxs ? '<slide>.mrxs' : '<slide>.vms';
    const missing = Array.isArray(info.missing) ? info.missing.filter(Boolean) : [];
    const onlyEntryMissing = missing.length === 1 && missing[0] === placeholder;
    // 文件夹里连主入口都没有：选文件夹只用于完整包，单文件应走「选择文件」
    if (onlyEntryMissing && ctx.kind === 'folder') {
      return t('tools.input.folder.not_bundle');
    }
    if (missing.length) {
      const sep = document.documentElement.lang === 'en' ? ', ' : '、';
      // 协助装配后的缺成员（复核 r3 A）：面板就在下方引导「重新选择「{stem}」
      // 文件夹」——错误里不再重复「选上一级文件夹」的入门指引（两者矛盾）
      if (isMrxs && page.companion) {
        return t('tools.err.bundle_incomplete.mrxs.retry',
          { missing: missing.join(sep) });
      }
      return t(isMrxs ? 'tools.err.bundle_incomplete.mrxs'
        : 'tools.err.bundle_incomplete.vms', { missing: missing.join(sep) });
    }
  }
  return friendlyError(e);
}

// ------------------------------------------------------------ i18n glue --

document.addEventListener('hp-lang-change', rerenderForLang);

function rerenderForLang() {
  document.title = t('tools.doc.title');
  updateProfileSuggestText();
  renderOutputFormatSection();
  renderQualitySection();
  renderSaveStatus();
  renderResultPanel();
  renderJobs(page.lastJobs || []);
  if (page.prep) {
    renderSummary();
    renderProbeSummary();
  }
  if (page.busyPhaseLabelKey) els.runStatus.textContent = t(page.busyPhaseLabelKey);
  renderRunJob();
  if (stageMsg.key) els.stageStatus.textContent = t(stageMsg.key, stageMsg.vars);
  if (statusMsg.key) els.pageStatus.textContent = t(statusMsg.key, statusMsg.vars);
  if (inputMsg.key) els.inputMessage.textContent = t(inputMsg.key, inputMsg.vars);
  if (companionMsg.key && !els.companionSection.hidden) {
    els.companionMessage.textContent = t(companionMsg.key, companionMsg.vars);
  }
  if (companionPickMsg.key && !els.companionSection.hidden) {
    els.companionPickBtn.textContent = t(companionPickMsg.key, companionPickMsg.vars);
  }
  if (companionHintMsg.key && !els.companionSection.hidden) {
    els.companionHint.textContent = t(companionHintMsg.key, companionHintMsg.vars);
  }
  if (page.directHint) renderDirectHint();
  if (runBytesMsg.key && !els.runBytes.hidden) els.runBytes.textContent = t(runBytesMsg.key, runBytesMsg.vars);
  updateStrictWarning();
}

// ------------------------------------------- 保存能力缺失（新转换闸） --
// 2026-10-20：无 File System Access 保存 API 的浏览器没有产出物的出口——
// 不让用户把时间花在「转换成无法保存的东西」上：禁用**新**转换的全部入口
// （选择文件/文件夹、拖放），就地显示工作台指引（带普通「打开工作台」
// 链接，不带文件/意图）。既有任务的查看与删除不受影响。
function saveUnsupported() {
  return typeof window.showSaveFilePicker !== 'function';
}

/// 把保存能力缺失的指引写进 #save-capability-note（文字 + 打开工作台
/// 链接；DOM 构造，不用 innerHTML）。renderSaveStatus 与入口闸共用。
function paintSaveUnsupportedNote() {
  const note = els.saveCapabilityNote;
  if (!note || !note.appendChild) return;
  note.textContent = '';
  note.appendChild(document.createTextNode(t('tools.input.save.unsupported') + ' '));
  const a = document.createElement('a');
  a.href = '/app';
  a.textContent = t('tools.open.workbench');
  note.appendChild(a);
}

/// 页面级忙态（P1-4 / P2-6 / P2-12）：准备一开始就锁住全部入口与新转换
/// 动作，直到终态；任务列表的开始/续跑期间同样生效。既有任务的查看/删除
/// 不受影响（删除运行中的任务会先停止它，见 P2-7）。
function applyBusyState() {
  const blocked = page.busy || saveUnsupported();
  for (const btn of [els.pickFileBtn, els.pickFolderBtn,
    els.companionPickBtn, els.companionCancelBtn]) {
    if (!btn) continue;
    btn.disabled = blocked;
    btn.setAttribute('aria-disabled', String(blocked));
  }
  if (els.fileInput) els.fileInput.disabled = blocked;
  if (els.folderInput) els.folderInput.disabled = blocked;
  if (els.channelInput) els.channelInput.disabled = page.busy;
  if (els.dropZone) {
    els.dropZone.classList.toggle('drop-zone-busy', page.busy);
    els.dropZone.classList.toggle('drop-zone-disabled', saveUnsupported());
    els.dropZone.setAttribute('aria-disabled', String(blocked));
  }
  if (els.convertBtn) els.convertBtn.disabled = page.busy || !page.prep;
  // 任务列表：忙碌时只有「开始/续跑」被禁用；删除仍可用（先停止）
  for (const btn of document.querySelectorAll('.job-actions button[data-action]')) {
    const action = btn.dataset.action;
    if (action === 'start' || action === 'resume') btn.disabled = page.busy;
  }
}

function setPageBusy(on) {
  const wasBusy = page.busy;
  page.busy = !!on;
  // 忙碌结束后撤掉「正在转换中…」的拒绝提示，避免任务已完成仍显示旧状态
  if (wasBusy && !page.busy && inputMsg.key === 'tools.drop.busy') clearInputMessage();
  applyBusyState();
}

/// 禁用新转换入口（按钮/拖放区）；既有任务的查看/删除照常。
function disableNewConversionInputs() {
  applyBusyState();
}

function guardNewConversion() {
  if (!saveUnsupported()) return false;
  paintSaveUnsupportedNote();
  showInputMessage('tools.input.save.unsupported');
  return true;
}

// ------------------------------------------------- MRXS 同名文件夹协助 --
//
// 网页拿不到 .mrxs 旁边的同级目录：散文件输入里只有一个 .mrxs（可能有散装
// .dat，但没有数据文件夹）时，不进 planner、不复制任何内容，先请用户选择
// 同名数据文件夹（prepareBundleSource 里的 detectNeedsFolder 分流）。
//   - showDirectoryPicker 可用：{ id, mode:'read' } + startIn=拖入 .mrxs 的
//     文件句柄（drop 事件内同步 getAsFileSystemHandle 捕获）；TypeError 回
//     退重试，AbortError=用户取消（安静保持待选状态）；
//   - 无 API/SecurityError：回退到既有隐藏 webkitdirectory input
//     （onFolderPicked 的协助分支），iOS 等以提示如实说明；
//   - 选完后由共享模块判定 (a)同名文件夹 (b)父文件夹里的同名子目录
//     (c)不同名但根上有 Slidedat.ini（<dialog> 显式确认）(d)没有 Slidedat.ini
//     （明确拒绝并请重选），装配后的行走既有 planner 路径。

function hideCompanionPanel() {
  if (els.companionSection) els.companionSection.hidden = true;
  companionMsg.key = null;
  companionMsg.vars = null;
  companionPickMsg.key = null;
  companionPickMsg.vars = null;
  companionHintMsg.key = null;
  companionHintMsg.vars = null;
}

function clearCompanion() {
  page.companion = null;
  hideCompanionPanel();
}

/// 面板渲染（首次出现与缺成员失败后重出共用；按钮聚焦，正文在 aria-live 区）。
/// retry=true：用户已补选过文件夹、但 planner 仍报包不完整——正文与按钮都
/// 换成「重新选择」口径（不重复入门提示）。
/// 提示行（两种路径都显示）：说明浏览器会请求读取文件夹的权限，并强调文件
/// 只在本机处理（Chrome 回退对话框的按钮字面就叫「上传」，必须主动澄清）。
/// 只有连 webkitdirectory 都没有时才宣布「无法选择文件夹」并隐藏主按钮
///（不留死按钮）。
function paintCompanionPanel(retry = false) {
  const comp = page.companion;
  if (!comp) return;
  els.companionSection.hidden = false;
  setCompanionMsg(retry ? 'tools.companion.needed.retry' : 'tools.companion.needed',
    { stem: comp.stem });
  setCompanionPick(retry ? 'tools.companion.pick.retry' : 'tools.companion.pick',
    { stem: comp.stem });
  const C = companionModule();
  const pickerSupported = !!(C && typeof C.supportsDirectoryPicker === 'function'
    && C.supportsDirectoryPicker());
  const folderInputSupported = !C || typeof C.supportsFolderPicker !== 'function'
    ? true : !!C.supportsFolderPicker();
  if (!pickerSupported && !folderInputSupported) {
    if (els.companionPickBtn) els.companionPickBtn.hidden = true;
    setCompanionHint('tools.companion.unsupported');
  } else {
    if (els.companionPickBtn) els.companionPickBtn.hidden = false;
    setCompanionHint('tools.companion.hint');
  }
  els.companionPickBtn.focus();
}

function showCompanion(need, opts = {}) {
  resetFlowPanels();          // 清掉上一个任务的摘要/错误；协助状态由我们接管
  els.fileInput.value = '';
  els.folderInput.value = '';
  page.file = null;
  page.bundleFiles = null;
  page.bundleFolderName = null;
  page.prepareContext = null;
  // 拖入的 .mrxs 可带 FileSystemFileHandle（drop 事件内同步捕获的 Promise）：
  // 只用于 showDirectoryPicker 的 startIn 定位，用户仍需显式确认文件夹
  const handles = opts.fileHandles;
  const handle = (handles && typeof handles.get === 'function')
    ? (handles.get(need.entryRow.file) || null) : null;
  page.companion = {
    stem: need.stem,
    entryName: need.entryName,
    file: need.entryRow.file,
    handle,
  };
  paintCompanionPanel();
}

/// 「选择「{stem}」文件夹」按钮：目录选择器优先，缺 API/SecurityError 回退
/// 到 webkitdirectory input（同手势内 click）。
async function onCompanionPick() {
  const comp = page.companion;
  if (!comp) return;
  const C = companionModule();
  if (!C || typeof C.pickDirectory !== 'function'
      || !C.supportsDirectoryPicker()) {
    els.folderInput.click();
    return;
  }
  let startIn = null;
  if (comp.handle) {
    try { startIn = await comp.handle; } catch { startIn = null; }
  }
  const res = await C.pickDirectory({ id: 'mrxs-companion', startIn });
  if (res.ok) {
    await onCompanionFolderHandle(res.handle, res.name);
    return;
  }
  if (res.reason === 'cancelled') return;   // 用户取消：安静保持待选状态
  els.folderInput.click();                  // security/error：回退输入框
}

/// showDirectoryPicker 结果的遍历（复核 2026-10-22：picked 目录不同名时只
/// 遍历同名直接子目录——多切片的父文件夹不再整树遍历/撞上限；成员行 relPath
/// 仍以 picked 目录名开头，判定/装配不变）。失败如实说明并保持在待选态。
async function onCompanionFolderHandle(handle, folderName) {
  const C = companionModule();
  if (!page.companion || !C || !handle) return;
  let rows = null;
  try {
    rows = await C.collectCompanionFolder(handle, page.companion.stem,
      { maxMembers: E.MRXS_MAX_MEMBERS });
  } catch (e) {
    console.warn('companion traversal failed', e);
    setCompanionMsg('tools.companion.read_failed');
    return;
  }
  await companionFolderResolved(rows, folderName || handle.name || '');
}

/// 补选文件夹的统一判定漏斗（目录句柄与 webkitdirectory 回退共用）：
/// (a)/(b) 装配 → 既有 planner；(c) <dialog> 显式确认后装配；(d) 明确拒绝。
async function companionFolderResolved(rows, folderName) {
  const comp = page.companion;
  const C = companionModule();
  if (!comp || !C) return;
  const plan = C.planCompanionFolder({ stem: comp.stem, folderName, rows });
  if (plan.status === 'use' || plan.status === 'parent') {
    await runCompanionMerged(rows, plan.prefixSegs, comp.stem);
    return;
  }
  if (plan.status === 'mismatch') {
    const names = { folder: plan.folderName || folderName, stem: comp.stem };
    const ok = await askDiskConfirm({
      title: t('tools.companion.mismatch.title', names),
      body: t('tools.companion.mismatch.body', names),
      confirmLabel: t('tools.companion.mismatch.use'),
      denyLabel: t('tools.companion.mismatch.reselect'),
    });
    if (ok) {
      await runCompanionMerged(rows, plan.prefixSegs, comp.stem);
      return;
    }
    // 重新选择：回到「需要文件夹」的待选文案
    setCompanionMsg('tools.companion.needed', { stem: comp.stem });
    return;
  }
  // (d)：不是这张切片的数据文件夹——明确说明，保持按钮可重选
  setCompanionMsg('tools.companion.not_data_folder',
    { folder: folderName, stem: comp.stem });
}

/// 装配（入口 = 已选 .mrxs File；成员 relPath 改写为「<stem>/…」）→ 既有
/// prepareBundleSource → planner。仍然没有复制任何内容：planner 缺成员会
/// 在复制前类型化拒绝（届时协助面板重出，按钮即「重新选择文件夹」）。
async function runCompanionMerged(rows, prefixSegs, stem) {
  const comp = page.companion;
  const C = companionModule();
  if (!comp || !C) return;
  const merged = C.mergeRows({
    stem,
    entryName: comp.entryName,
    entryFile: comp.file,
    rows,
    prefixSegs,
    maxMembers: E.MRXS_MAX_MEMBERS,
  });
  if (!merged.ok) {
    setCompanionMsg(merged.code === 'too_many_members'
      ? 'tools.companion.too_many' : 'tools.companion.read_failed',
      { n: String(merged.cap || E.MRXS_MAX_MEMBERS) });
    return;
  }
  hideCompanionPanel();
  await prepareBundleSource(merged.rows, { folderName: stem, fromFolder: true });
}

// ------------------------------------------------------------- dialogs --

/// uncertain 磁盘确认：返回 true（确认继续）/ false（取消）。焦点进入对话框，
/// 关闭后还原到打开者（键盘可达；Esc = 取消）。U2：该确认是独立模态对话框，
/// 永不被折叠或隐藏。
function askDiskConfirm({ title, body, confirmLabel, denyLabel }) {
  return new Promise((resolve) => {
    const opener = document.activeElement;
    els.diskTitle.textContent = title;
    els.diskBody.textContent = body;
    els.diskConfirm.textContent = confirmLabel || t('tools.disk.confirm');
    els.diskDeny.textContent = denyLabel || t('tools.disk.cancel');
    const done = (answer) => {
      els.diskDialog.removeEventListener('close', onClose);
      els.diskDialog.close();
      els.diskDialog.removeEventListener('cancel', onCancel);
      if (opener && opener.focus) opener.focus();
      resolve(answer);
    };
    const onClose = () => {
      const a = els.diskDialog.returnValue === 'confirm';
      els.diskDialog.removeEventListener('cancel', onCancel);
      if (opener && opener.focus) opener.focus();
      resolve(a);
    };
    const onCancel = (ev) => { ev.preventDefault(); done(false); };
    els.diskDialog.addEventListener('close', onClose);
    els.diskDialog.addEventListener('cancel', onCancel);
    els.diskDialog.showModal();
    els.diskDeny.focus();
  });
}

async function probeWithDiskFlow(file, opts = {}) {
  const confirmOverride = opts.confirmUncertainDisk || false;
  try {
    return await page.runner.probe(file, {
      confirmUncertainDisk: confirmOverride, channelJson: page.channelJson,
      outputProfile: opts.outputProfile,
      encodingProfile: opts.encodingProfile,
    });
  } catch (e) {
    if (errCode(e) === 'disk_precheck_failed') {
      const info = (e && e.error) || {};
      if (info.uncertain) {
        const ok = await askDiskConfirm({
          title: t('tools.disk.title'),
          body: t('tools.disk.body', {
            need: fmtBytes(info.need && info.need.total),
            available: fmtBytes(info.available),
          }),
        });
        if (!ok) throw e;
        return await page.runner.probe(file, {
          confirmUncertainDisk: true, channelJson: page.channelJson,
          outputProfile: opts.outputProfile,
          encodingProfile: opts.encodingProfile,
        });
      }
    }
    throw e;
  }
}

// ------------------------------------------------------- prepareSource --
//
// file input 与 drop 的唯一汇合点（计划 §3.2.1）。一次只接受一个切片文件：
// 多文件 → 明确提示、什么都不开始；含 .mrxs/.dat（MRXS）或 .vms/.vmu
// （VMS/VMU）的输入（单独主文件、散装成员或恰好完整的包）一律交给束包
// planner（engine.planBundle 按入口类型分派，经 runner.prepareBundle）：
// 缺成员 → 类型化信息列出还差什么，任何复制之前拒绝——绝不静默丢弃。
// 扩展名只是提示：真正的识别在运行器的文件头魔数检查（_prepare 在完整
// 复制之前拒绝已知不支持的文件）。

async function prepareSource(fileList, opts = {}) {
  if (guardNewConversion()) return;   // 无保存出口：不开始会做无用功的转换
  const route = bundleRoute(fileList);
  if (route.route === 'empty') return;
  if (page.busy) {
    // 准备或转换进行中不接受新输入：resetFlowPanels 会撤掉进行中的进度 UI
    //（file input 已禁用，这里补上 drop 路径的同等防护）
    showInputMessage('tools.drop.busy');
    return;
  }
  beginInputAttempt();   // P2-9：被拒绝的输入也从干净状态开始
  if (route.route === 'bundle') {
    // .mrxs/.dat（MRXS）或 .vms/.vmu（VMS/VMU）（单个或散装多个）→ 同一
    // planner 分派；文件夹/目录 drop 见 handleDropData（带相对路径的完整
    // 包在这里被接受）。fileHandles：drop 事件内同步捕获的文件句柄
    //（File → Promise<FileSystemFileHandle>），协助流程用它定位 startIn
    await prepareBundleSource(route.files, { fileHandles: opts.fileHandles });
    return;
  }
  if (route.route === 'multiple') {
    showInputMessage('tools.drop.multiple', { n: String(route.files.length) });
    return;
  }
  await onFilePicked(route.file);
}

/// classifyFile 的容错包装（嗅探失败按 null 处理——走既有转换流程）。
async function sniffOrNull(file) {
  try {
    if (window.HP_SLIDE_SNIFF &&
        typeof window.HP_SLIDE_SNIFF.classifyFile === 'function') {
      return await window.HP_SLIDE_SNIFF.classifyFile(file);
    }
  } catch { /* 嗅探失败不阻塞既有流程 */ }
  return null;
}

/// 已兼容的文件（OME-TIFF / 本工具导出的 BigTIFF）：无需转换，提示可直接
/// 使用；上传请前往工作台（本页不提供上传）。点击之前零 /api 请求（C3 不变）。
/// 文案按 i18n 键重建：切换语言时 rerenderForLang 调 renderDirectHint（P3-14）。
function renderDirectHint() {
  els.inputMessage.textContent = '';
  const line = document.createElement('span');
  line.textContent = t('tools.direct.hint') + ' ';
  const a = document.createElement('a');
  a.href = '/app';
  a.textContent = t('tools.to.workbench');
  els.inputMessage.appendChild(line);
  els.inputMessage.appendChild(a);
}

function showDirectHint(file) {
  resetFlowPanels();
  els.fileInput.value = '';
  els.folderInput.value = '';
  page.file = file;
  page.prepareContext = { kind: 'file', name: file.name };
  page.directHint = true;
  inputMsg.key = null;
  inputMsg.vars = null;
  renderDirectHint();
}

/// drop 事件取目录项只能在事件处理器内同步做（webkitGetAsEntry 的有效
/// 窗口）。目录 drop：递归遍历（readEntries 每批 ≤100，循环到空批）构造
/// 带相对路径的成员行，进入与「选择文件夹（MRXS）」相同的
/// prepareBundleSource 流程；浏览器无法遍历时提示改用该按钮。
function handleDropData(dt) {
  if (!dt) return;
  if (page.busy) {   // 忙碌时不做目录遍历：先给出可读提示
    showInputMessage('tools.drop.busy');
    return;
  }
  const items = dt.items ? Array.from(dt.items) : [];
  const dirEntries = [];
  // FileSystemFileHandle 只能在 drop 事件内同步索取（getAsFileSystemHandle）：
  // 拖入散文件时把句柄按 File 存下，之后 showDirectoryPicker({startIn}) 用它
  // 把目录选择器定位到 .mrxs 所在位置（仅定位；用户仍需确认）
  const fileHandles = new Map();
  for (const it of items) {
    try {
      const entry = it.webkitGetAsEntry && it.webkitGetAsEntry();
      if (entry && entry.isDirectory) dirEntries.push(entry);
    } catch { /* 非 filesystem 条目：按普通文件处理 */ }
    if (it.kind === 'file' && typeof it.getAsFileSystemHandle === 'function') {
      try {
        const f = typeof it.getAsFile === 'function' ? it.getAsFile() : null;
        const h = it.getAsFileSystemHandle();
        if (f && h) fileHandles.set(f, h);
      } catch { /* 句柄不可用：startIn 定位不可用，不影响其余流程 */ }
    }
  }
  if (dirEntries.length) {
    void onDirectoryDropped(dirEntries);
    return;
  }
  prepareSource(dt.files ? Array.from(dt.files) : [], { fileHandles });
}

/// 目录 drop 的遍历与接管（webkitGetAsEntry 只能在事件内同步取，取到的
/// entry 对象此后仍可异步读取）。
async function onDirectoryDropped(dirEntries) {
  if (guardNewConversion()) return;
  if (page.busy) {
    showInputMessage('tools.drop.busy');
    return;
  }
  beginInputAttempt();
  if (dirEntries.length > 1) {
    showInputMessage('tools.drop.bundle.multiple', { n: String(dirEntries.length) });
    return;
  }
  const entry = dirEntries[0];
  try {
    const rows = await collectEntryFiles(entry, { maxMembers: E.MRXS_MAX_MEMBERS });
    if (!rows.length) {
      showInputMessage('tools.input.folder.empty');
      return;
    }
    await prepareBundleSource(rows, { folderName: entry.name, fromFolder: true });
  } catch (e) {
    // 无法遍历（旧浏览器 API 缺失/读取失败/成员数超上限）：指向文件夹按钮
    console.warn('drop traversal failed', e);
    showInputMessage('tools.drop.dir.unreadable');
  }
}

// -------------------------------------------------- bundle source (F3) --
//
// MRXS 完整包输入的唯一汇合点：webkitdirectory 文件夹选择、目录 drop 遍历、
// 以及 .mrxs/.dat 散文件都进这里，委托 runner.prepareBundle（复制前 planner
// 决定接受/缺成员拒绝；成员逐个复制到 OPFS 并全程按字节显示进度；清单
// 落盘后续跑不再需要原文件夹）。阶段 UI/磁盘确认/取消准备与单文件相同。

async function prepareBundleSource(files, opts = {}) {
  if (page.busy) {
    showInputMessage('tools.drop.busy');
    return;
  }
  const rows = bundleRows(files);
  clearInputMessage();
  if (!rows.length) {
    beginInputAttempt();
    showInputMessage('tools.input.folder.empty');
    return;
  }
  // MRXS 协助（2026-10）：散文件输入（不含文件夹选择/目录 drop，也不含协助
  // 装配后的再次进入）里只有一个 .mrxs、没有任何目录结构 → 先请用户补选
  // 同名数据文件夹，不进 planner、不复制任何内容
  if (!opts.fromFolder && !page.companion) {
    const C = companionModule();
    const need = (C && typeof C.detectNeedsFolder === 'function')
      ? C.detectNeedsFolder(rows) : null;
    if (need) {
      showCompanion(need, opts);
      return;
    }
  }
  // 离开上一个未完成任务：留在任务列表里可续跑/删除
  resetFlowPanels();
  // 清空文件夹选择器，保证再次选择同一目录仍触发 change；file input 不动
  //（其 files 在处理期间保持可读，清空会破坏进行中的读取约定）
  els.folderInput.value = '';
  page.file = null;
  page.channelJson = null;
  page.channelJsonName = null;
  page.bundleFiles = rows;
  page.bundleFolderName = opts.folderName
    || folderNameFromRelPath(rows.length ? rows[0].relPath : '');
  page.prepareContext = {
    kind: (opts.fromFolder || page.bundleFolderName) ? 'folder' : 'bundle',
    name: page.bundleFolderName || rows[0].name,
  };
  await runBundleProbeFlow();
}

/// runner.prepareBundle + uncertain 磁盘确认（与 probeWithDiskFlow 同一合同：
/// 报告封顶无法证明空间时问用户，确认后带 confirmUncertainDisk 重试）。
async function prepareBundleWithDiskFlow(rows, opts = {}) {
  const call = (confirmUncertainDisk) => page.runner.prepareBundle(rows, {
    confirmUncertainDisk,
    outputProfile: opts.outputProfile,
    encodingProfile: opts.encodingProfile,
    folderName: opts.folderName,
  });
  try {
    return await call(opts.confirmUncertainDisk || false);
  } catch (e) {
    if (errCode(e) === 'disk_precheck_failed') {
      const info = (e && e.error) || {};
      if (info.uncertain) {
        const ok = await askDiskConfirm({
          title: t('tools.disk.title'),
          body: t('tools.disk.body', {
            need: fmtBytes(info.need && info.need.total),
            available: fmtBytes(info.available),
          }),
        });
        if (!ok) throw e;
        return await call(true);
      }
    }
    throw e;
  }
}

/// 完整包准备流程：MRXS 恒为明场（荧光包由核心在探测时类型化拒绝），
/// 直接带上当前输出/画质选择；无伴随文件概念。
async function runBundleProbeFlow() {
  const rows = page.bundleFiles || [];
  await runPrepareFlow(async () => prepareBundleWithDiskFlow(rows, {
    outputProfile: selectedOutputProfile(),
    encodingProfile: selectedEncodingProfile(),
    folderName: page.bundleFolderName,
  }), rows.reduce((a, r) => a + (r.file.size || 0), 0));
}

function resetFlowPanels() {
  clearError();
  clearInputMessage();
  page.prep = null;
  page.readyInfo = null;
  page.saveMsg = null;
  page.directHint = false;
  page.bundleFiles = null;
  page.bundleFolderName = null;
  page.prepareContext = null;
  page.outputLockedProfile = null;
  page.encodingLockedProfile = null;
  // 协助面板只是隐藏，不清 page.companion：planner 缺成员失败后要靠它重出
  // 「重新选择文件夹」面板（新输入尝试走 beginInputAttempt → clearCompanion）
  hideCompanionPanel();
  for (const el of [els.probeSection, els.estimateSection, els.profileSection,
    els.policySection, els.formatSection, els.resultSection, els.summarySection,
    els.channelSection]) el.hidden = true;
  els.runSection.hidden = true;
  els.convertBtn.disabled = true;
  els.runStatus.textContent = '';
  setRunJob(null, null);
  els.runProgress.hidden = true;
  els.runBytes.hidden = true;
  els.stageSection.hidden = true;
  els.prepareCancelBtn.hidden = true;
  els.qualityFieldset.hidden = true;
  els.qualityFieldset.disabled = false;
  els.qualityMrsxNote.hidden = true;
  els.qualityNdpiNote.hidden = true;
  els.qualityVmsNote.hidden = true;
  els.qualityLocked.hidden = true;
  els.qualityConflict.hidden = true;
  els.moreOptions.open = false;
  const preserve = document.getElementById('quality-preserve');
  if (preserve) preserve.checked = true;
}

async function onFilePicked(explicitFile) {
  const list = els.fileInput.files;
  const file = explicitFile || (list && list[0]);
  // P1-2：捕获 File 后立即清空 input —— 再次选择同一路径仍会触发 change；
  // 已捕获的 File 对象不受 input.value 重置影响。放在保存能力闸之前，
  // 被拒绝的选择同样不会把旧值留在 input 里。
  els.fileInput.value = '';
  if (guardNewConversion()) return;   // 无保存出口：file input 的 change 直达这里
  if (!file) return;
  if (page.busy) {
    showInputMessage('tools.drop.busy');
    return;
  }
  beginInputAttempt();
  // 一次一个切片文件（U2 第一版）：多选只提示、不静默丢弃
  if (!explicitFile && list && list.length > 1) {
    showInputMessage('tools.drop.multiple', { n: String(list.length) });
    return;
  }
  // 单独的 .mrxs / .dat（file input 手动选到）也交给 planner：缺成员的
  // 类型化信息在任何复制之前出现
  if (E.inputExtensionHint(file.name) === 'bundle') {
    await prepareBundleSource([file]);
    return;
  }
  // P2-8：压缩包格式中立地说明「先解压」——不再落进魔数拒绝的
  // 「不是本工具支持的 KFB/KFBF 文件」
  if (ARCHIVE_RE.test(file.name)) {
    showInputMessage('tools.input.archive.unsupported');
    return;
  }
  // 已兼容格式（OME-TIFF / 本工具导出的 BigTIFF）不进转换——提示可直接
  // 使用、上传前往工作台（嗅探只读 ≤128KB 头；分类表见 slide-sniff.js）。
  // input 与 drop 的唯一汇合点在此。
  const directCls = await sniffOrNull(file);
  if (directCls && (directCls.cls === 'ome-tiff'
                    || directCls.cls === 'converter-bigtiff')) {
    showDirectHint(file);
    return;
  }
  resetFlowPanels();
  page.file = file;
  page.prepareContext = { kind: 'file', name: file.name };
  await readChannelJsonInput();
  await runProbeFlow();
}

/// 「选择文件夹」：webkitdirectory input 的 change —— File 的
/// webkitRelativePath 已带文件夹前缀（engine 据此定位包根），交给
/// prepareBundleSource（与目录 drop 完全相同的流程）。
/// 协助流程激活时（page.companion，目录选择器不可用浏览器的回退路径）改道：
/// 这次选择是「补选同名数据文件夹」，走判定/装配漏斗而不是整包入口。
async function onFolderPicked() {
  const list = els.folderInput.files;
  const files = list ? Array.from(list) : [];
  // P1-2：捕获后清空，同一文件夹可再次选择（被闸拒绝时也不留旧值）
  els.folderInput.value = '';
  if (guardNewConversion()) return;
  if (page.busy) {
    showInputMessage('tools.drop.busy');
    return;
  }
  if (page.companion) {
    if (!files.length) return;   // 选择器被取消：保持「待选文件夹」状态
    const first = (files[0].webkitRelativePath || files[0].name || '');
    const folderName = first.split('/')[0] || files[0].name;
    await companionFolderResolved(bundleRows(files), folderName);
    return;
  }
  beginInputAttempt();
  if (!files.length) {
    showInputMessage('tools.input.folder.empty');   // 空文件夹此前静默
    return;
  }
  await prepareBundleSource(files, { fromFolder: true });
}

/// 协助面板的「取消」：清掉待选状态，回到干净输入态（选别的文件/文件夹）
function onCompanionCancel() {
  clearCompanion();
  setPageMsg(null);
}

async function readChannelJsonInput() {
  page.channelJson = null;
  page.channelJsonName = null;
  const f = els.channelInput.files && els.channelInput.files[0];
  if (!f) return;
  if (f.size > CHANNEL_JSON_MAX_BYTES) {
    setPageMsg('tools.channel.too.large');
    return;
  }
  try {
    page.channelJson = await f.slice(0, CHANNEL_JSON_MAX_BYTES).text();
    page.channelJsonName = f.name;
  } catch {
    page.channelJson = null;
    setPageMsg('tools.channel.read.failed');
  }
}

// 已准备（未开始）的任务：伴随文件改选后同步到任务记录，刷新后从列表开始仍带上它
async function onChannelPicked() {
  await readChannelJsonInput();
  if (!page.prep || page.busy) return;
  try {
    await page.runner.setPreparedChannelJson(page.prep.jobId, page.channelJson);
  } catch (e) {
    showError(e);
  }
}

async function runProbeFlow() {
  await runPrepareFlow(async () => {
    const cjAtProbe = page.channelJson;
    // 输出格式与画质选择在准备时就传给运行器（prepared 记录反映用户选择）。
    // 模态在复制+探测后才能确认，这里用文件头嗅探（与核心同源的魔数表）
    // 预判：只有明场才传（荧光固定 fl-ome / 保留画质，运行器按模态默认）。
    const head = new Uint8Array(await page.file.slice(0, 8).arrayBuffer());
    const sniffedModality = E.magicModality(head);
    const prep = await probeWithDiskFlow(page.file, {
      outputProfile: sniffedModality === 'brightfield' ? selectedOutputProfile() : undefined,
      encodingProfile: sniffedModality === 'brightfield' ? selectedEncodingProfile() : undefined,
    });
    // 伴随文件在复制/探测期间改选过：补写进任务记录
    if (page.channelJson !== cjAtProbe) {
      await page.runner.setPreparedChannelJson(prep.jobId, page.channelJson);
    }
    return prep;
  }, page.file.size);
}

/// 准备（复制与识别）的共用驱动：单文件 runProbeFlow 与完整包
/// runBundleProbeFlow 共享同一阶段 UI/取消/磁盘确认/错误与摘要收尾。
/// `doPrepare` 返回 prep（{jobId, probe, identity?}），totalBytes 用于进度。
async function runPrepareFlow(doPrepare, totalBytes) {
  clearError();
  setPageMsg(null);
  setPageBusy(true);   // 准备一开始就锁定全部新输入入口（P1-4 / P2-6）
  els.stageSection.hidden = false;
  setStageMsg('tools.stage.status');
  setProgress(els.stageProgress, els.stageBar, els.stageBytes, 0, `0 / ${fmtBytes(totalBytes)}`);
  setBeforeunload(true);
  els.prepareCancelBtn.hidden = false;
  els.prepareCancelBtn.disabled = false;
  let prep;
  try {
    prep = await doPrepare();
    page.prep = prep;
    page.companion = null;   // 准备成功：协助流程收口
  } catch (e) {
    els.prepareCancelBtn.hidden = true;
    setBeforeunload(false);
    setStageMsg(null);
    els.stageSection.hidden = true;   // 失败/取消都不留空白准备卡（P3-15）
    const code = errCode(e);
    const info = (e && e.error) || {};
    if (code === 'cancelled') {
      // 取消准备（U2）：worker 已终止、任务目录已删除——明确反馈，不算错误
      page.file = null;
      page.bundleFiles = null;
      page.bundleFolderName = null;
      page.prepareContext = null;
      page.companion = null;
      setPageMsg('tools.stage.cancelled');
      setPageBusy(false);
      refreshJobs();
      return;
    }
    if (code === 'disk_precheck_failed') {
      if (info.uncertain) {
        // 用户在「无法证明空间够用」对话框里取消：中性状态，不是红色错误
        page.file = null;
        page.bundleFiles = null;
        page.bundleFolderName = null;
        page.prepareContext = null;
        page.companion = null;
        setPageMsg('tools.disk.cancelled.note');
      } else {
        showErrorText(t('tools.disk.hard.title') + '\n' + t('tools.disk.hard.body', {
          need: fmtBytes(info.need && info.need.total),
          available: fmtBytes(info.available),
        }) + `\n(${E.errText(e)})`);
      }
      page.companion = null;
    } else {
      showErrorText(prepareErrorMessage(e));
      // 协助装配后 planner 仍报 MRXS 包问题（缺成员等）：面板以「重新选择」
      // 口径重出（正文不再重复入门提示，按钮 = 重新选择「{stem}」文件夹），
      // 既有类型化错误照常显示在上方
      if (page.companion && code === 'unsupported_input'
          && info.kind === 'mrxs-bundle') {
        paintCompanionPanel(true);
      } else {
        page.companion = null;
      }
    }
    setPageBusy(false);
    refreshJobs();
    return;
  }
  els.prepareCancelBtn.hidden = true;
  setBeforeunload(false);
  setStageMsg('tools.stage.done');
  setPageMsg(null);   // 成功的准备清掉上一个任务的「已取消」等旧状态（P2-9）
  const copiedBytes = Number.isFinite(prep.identity && prep.identity.size)
    ? prep.identity.size : totalBytes;
  setProgress(els.stageProgress, els.stageBar, els.stageBytes, 1,
    `${fmtBytes(copiedBytes)} / ${fmtBytes(totalBytes)}`);
  // 识别完成：一张配置摘要 + 折叠的高级项（荧光只隐藏不适用的选择组）
  els.summarySection.hidden = false;
  els.probeSection.hidden = false;
  els.estimateSection.hidden = false;
  els.profileSection.hidden = false;
  els.policySection.hidden = false;
  renderOutputFormatSection();
  renderQualitySection();
  renderChannelSection();
  renderSummary();
  renderProbeSummary();
  els.runSection.hidden = false;
  applyProfileSuggestion();
  updateStrictWarning();
  setPageBusy(false);   // 已就绪：解锁选择入口，开始转换按钮可用
  refreshJobs();
}

function probeDoc() {
  return page.prep && page.prep.probe ? (page.prep.probe.document || {}) : {};
}

function probeEstimate() {
  const p = page.prep && page.prep.probe;
  if (!p) return {};
  return p.document && p.document.estimate ? p.document.estimate : (p.estimate || {});
}

// Summary card shows a reader-friendly family name; the core's format id stays
// in the technical summary under "更多选项".
function formatFamilyLabel(id) {
  const s = String(id || '');
  if (!s) return '—';
  if (s.startsWith('kfbf')) return 'KFBF';
  if (s.startsWith('kfb')) return 'KFB';
  if (s.startsWith('aperio-svs')) return 'SVS (Aperio)';
  if (s.startsWith('leica-scn')) return 'SCN (Leica)';
  if (s.startsWith('generic-tiled-jpeg-tiff')) return 'Generic TIFF';
  if (s.startsWith('mirax')) return 'MRXS';
  if (s.startsWith('hamamatsu-vms')) return 'VMS (Hamamatsu)';
  if (s.startsWith('hamamatsu-ndpi')) return 'NDPI (Hamamatsu)';
  if (s.startsWith('ventana-bif')) return 'BIF (Ventana)';
  if (s.startsWith('unic-tmap06')) return 'TMAP06（Unic，单文件明场）';
  if (s.startsWith('plain-image')) return '普通图片 (BMP/JPEG)';
  return s;
}

function renderSummary() {
  if (!page.prep) return;
  const doc = probeDoc();
  const identity = page.prep.identity || {};
  const g = els.summaryGrid;
  g.textContent = '';
  dlRow(g, t('tools.summary.name'),
    String(identity.name || (page.file && page.file.name) || '—'), 'summary-name');
  dlRow(g, t('tools.summary.size'),
    fmtBytes(Number.isFinite(identity.size) ? identity.size : (page.file ? page.file.size : NaN)),
    'summary-size');
  dlRow(g, t('tools.probe.format'), formatFamilyLabel(doc.format), 'summary-format');
  dlRow(g, t('tools.probe.modality'), doc.modality === 'fluorescence'
    ? t('tools.probe.modality.fl') : t('tools.probe.modality.bf'), 'summary-modality');
  renderOutputSummary();
}

function renderProbeSummary() {
  if (!page.prep) return;
  const doc = probeDoc();
  const est = probeEstimate();
  const isFL = doc.modality === 'fluorescence';
  const grid = els.probeGrid;
  grid.textContent = '';
  dlRow(grid, t('tools.probe.format'), doc.format || '—', 'probe-format');
  dlRow(grid, t('tools.probe.modality'), isFL ? t('tools.probe.modality.fl') : t('tools.probe.modality.bf'));
  dlRow(grid, t('tools.probe.dims'), `${doc.width || '?'} × ${doc.height || '?'} px`);
  const levels = (doc.levels || []).length;
  dlRow(grid, t('tools.probe.levels'), String(levels));
  if (isFL) {
    const names = (doc.channels || []).map((c) => c.name || `#${c.index}`).join('、');
    dlRow(grid, t('tools.probe.channels'), `${(doc.channels || []).length}（${names}）`);
  } else {
    dlRow(grid, t('tools.probe.channels'), t('tools.probe.channels.bf'));
  }
  // MPP：本体缺失就显示未知，绝不从 objective 猜测（计划 §2）
  const mppX = Number(doc.mpp_x);
  const mppY = Number(doc.mpp_y);
  const mpp = Number(doc.mpp);
  let mppText = t('tools.probe.mpp.unknown');
  if (isFL) {
    if (mpp > 0) mppText = `${mpp.toFixed(4)} µm/px`;
  } else if (mppX > 0 && mppY > 0) {
    mppText = mppX === mppY
      ? `${mppX.toFixed(4)} µm/px`
      : `${mppX.toFixed(4)} × ${mppY.toFixed(4)} µm/px`;
  }
  dlRow(grid, t('tools.probe.mpp'), mppText, 'probe-mpp');
  const edge = Number(est.edge_tiles || 0);
  dlRow(grid, t('tools.probe.edge'), edge > 0
    ? t('tools.probe.edge.note', { n: edge })
    : t('tools.probe.edge.none'), 'probe-edge');
  const missing = Number(est.cells_missing || 0);
  if (missing > 0) {
    dlRow(grid, t('tools.probe.missing'), t('tools.probe.missing.note', { n: missing }));
  }
  // F2: JP2K sources carry their codec identity in the probe document
  if (String(doc.codec || '') === 'jpeg2000-reencode') {
    dlRow(grid, t('tools.probe.sourcecodec'), t('tools.probe.sourcecodec.jp2k', {
      variant: String(doc.source_codec || '').replace('jpeg2000-', ''),
    }), 'probe-source-codec');
  }
  dlRow(grid, t('tools.probe.identity.sha'), String(page.prep.identity.sha256 || '—'));

  renderEstimate();
}

// 空间预估：源副本 + 输出（上界，随所选画质取对应编码的上界）+ 索引/日志
function renderEstimate() {
  if (!page.prep) return;
  const est = probeEstimate();
  const need = E.diskNeedBytes(est, {
    sourceBytes: page.prep.identity.size,
    encoding: selectedEncodingProfile(),
  });
  const eg = els.estimateGrid;
  eg.textContent = '';
  dlRow(eg, t('tools.estimate.source'), fmtBytes(need.source), 'estimate-source');
  dlRow(eg, t('tools.estimate.output'), fmtBytes(need.output), 'estimate-output');
  dlRow(eg, t('tools.estimate.scratch'), fmtBytes(need.scratch + need.journal));
  dlRow(eg, t('tools.estimate.total'), fmtBytes(need.total), 'estimate-total');
}

// ------------------------------------------------------- profile/policy --

function applyProfileSuggestion() {
  const suggested = E.defaultProfileId(navigator.deviceMemory, navigator.hardwareConcurrency);
  const radio = document.getElementById(`profile-${suggested}`);
  if (radio) radio.checked = true;
  updateProfileSuggestText();
}

function updateProfileSuggestText() {
  const suggested = E.defaultProfileId(navigator.deviceMemory, navigator.hardwareConcurrency);
  els.profileSuggest.textContent = t('tools.profile.suggest', {
    name: t(`tools.profile.${suggested}`),
    dm: navigator.deviceMemory === undefined ? '—' : String(navigator.deviceMemory),
  });
}

function selectedProfileId() {
  const checked = document.querySelector('input[name="profile"]:checked');
  return checked ? checked.value : E.defaultProfileId(navigator.deviceMemory);
}

function selectedPolicy() {
  const checked = document.querySelector('input[name="policy"]:checked');
  return checked ? checked.value : 'allow-edge';
}

// ---------------------------------------------------- output format (bf) --

/// 当前 UI 的明场输出格式选择（模板默认勾选 bf-ome；无 DOM 时也回落默认）。
function selectedOutputProfile() {
  const checked = document.querySelector('input[name="outputFormat"]:checked');
  return checked ? checked.value : E.OUTPUT_PROFILES.BF_OME;
}

/// 传给运行器的 outputProfile：只有确定是明场才传当前选择；荧光（以及模态
/// 未知的记录）传 undefined → 运行器按模态默认 fl-ome / 记录值，绝不把明场
/// 选择塞给荧光任务（runner 会以 kind=output-profile 拒绝）。
function outputProfileForModality(modality) {
  return modality === 'brightfield' ? selectedOutputProfile() : undefined;
}

/// 任务一旦开始（写入输出），该任务的输出格式不可再改：字段组禁用，改以
/// 文案展示任务实际格式。换选新文件（新任务）时在 resetFlowPanels 解锁。
function lockOutputProfile(profile) {
  page.outputLockedProfile = profile || null;
  renderOutputFormatSection();
}

function renderOutputFormatSection() {
  // 荧光输入不提供选择：固定多通道 OME-TIFF（fl-ome），整节隐藏。
  const show = !!page.prep && probeDoc().modality !== 'fluorescence';
  els.formatSection.hidden = !show;
  if (!show) return;
  const locked = page.outputLockedProfile;
  els.formatFieldset.disabled = !!locked;
  els.formatLocked.hidden = !locked;
  if (locked) {
    els.formatLocked.textContent = t('tools.format.locked', {
      name: t(`tools.result.format.${locked}`),
    });
  }
  renderOutputSummary();
}

/// 配置摘要的「输出」行：按目标软件表达，同时保留真实格式名 + 扩展名。
function renderOutputSummary() {
  if (!page.prep) return;
  const modality = probeDoc().modality;
  const profile = page.outputLockedProfile
    || (modality === 'brightfield' ? selectedOutputProfile() : E.defaultOutputProfile(modality));
  const ext = E.isOmeProfile(profile) ? '.ome.tif' : '.tif';
  els.outputSummaryText.textContent = t('tools.summary.output.line', {
    name: t(`tools.jobs.format.${profile}`),
    ext,
  });
}

/// 通道信息（可选 channel.json）只在荧光/KFBF 文件识别后出现（U2：识别前
/// 不问用户要伴随文件；明场输入整节隐藏）。
function renderChannelSection() {
  const show = !!page.prep && probeDoc().modality === 'fluorescence';
  els.channelSection.hidden = !show;
}

function updateStrictWarning() {
  const est = probeEstimate();
  const isJp2k = String(probeDoc().codec || '') === 'jpeg2000-reencode';
  // F2: a JPEG 2000 source has NO byte-passthrough path — strict-lossless is
  // always refused (the core's typed policy error is the backstop)
  const willRefuse = Number(est.edge_tiles || 0) > 0 || isJp2k;
  els.policyStrictWarn.hidden = !willRefuse;
  if (willRefuse) {
    els.policyStrictWarn.textContent = isJp2k
      ? t('tools.policy.strict.warn.jp2k')
      : t('tools.policy.strict.warn', { n: Number(est.edge_tiles || 0) });
  }
}

// ------------------------------------------------------- quality (U3) --

const ENCODING_LABEL_KEY = {
  [E.ENCODING_PROFILES.PRESERVE]: 'tools.quality.preserve',
  [E.ENCODING_PROFILES.COMPACT]: 'tools.quality.compact',
};

function encodingLabel(encoding) {
  const key = ENCODING_LABEL_KEY[encoding];
  return key ? t(key) : String(encoding || '—');
}

/// 当前 UI 的画质选择（模板默认勾选保留画质；无 DOM 时回落 preserve）。
function selectedEncodingProfile() {
  const checked = document.querySelector('input[name="encoding"]:checked');
  return checked ? checked.value : E.defaultEncodingProfile();
}

/// 传给运行器的 encodingProfile：只有确定是明场才传当前选择；荧光不传
/// （运行器按记录/默认 preserve——荧光不提供有损模式，与 outputProfile
/// 同一模式，runner 会以 kind=encoding-profile 拒绝不适用的值）。
function encodingProfileForModality(modality) {
  return modality === 'brightfield' ? selectedEncodingProfile() : undefined;
}

/// 任务一旦开始，画质不可再改：字段组禁用，改以文案展示任务实际画质。
function lockEncodingProfile(encoding) {
  page.encodingLockedProfile = encoding || null;
  renderQualitySection();
}

function renderQualitySection() {
  // 荧光不提供画质选择（compact 仅明场；保留画质是唯一语义）→ 整组隐藏。
  const show = !!page.prep && probeDoc().modality !== 'fluorescence';
  els.qualityFieldset.hidden = !show;
  updateMrsxQualityNote();
  applyQualityOptionDescriptions();
  if (!show) return;
  const locked = page.encodingLockedProfile;
  els.qualityFieldset.disabled = !!locked;
  els.qualityLocked.hidden = !locked;
  if (locked) {
    els.qualityLocked.textContent = t('tools.quality.locked', {
      name: encodingLabel(locked),
    });
  }
  updateQualityPolicyGate();
}

/// F3（MRXS）/F6（NDPI）/VMS 画质说明：三者的瓦片都是拼接/分段解码后
/// 重编码——「保留画质」是高质量重编码而非字节复制。仅 MRXS（mirax*）/
/// NDPI（hamamatsu-ndpi*）/VMS（hamamatsu-vms*）识别后显示对应说明行。
function updateMrsxQualityNote() {
  const fmt = String(probeDoc().format || '');
  const bf = probeDoc().modality !== 'fluorescence';
  els.qualityMrsxNote.hidden = !(!!page.prep && fmt.startsWith('mirax') && bf);
  els.qualityNdpiNote.hidden = !(!!page.prep && fmt.startsWith('hamamatsu-ndpi') && bf);
  els.qualityVmsNote.hidden = !(!!page.prep && fmt.startsWith('hamamatsu-vms') && bf);
  // F8（普通图片 BMP/JPEG）：像素必须解码后重编码——「保留画质」是
  // 高质量重编码而非字节复制
  els.qualityRasterNote.hidden = !(!!page.prep && fmt.startsWith('plain-image') && bf);
  // Ventana BIF：瓦片带重叠，必须拼接后重编码——「保留画质」是高质量
  // 重编码而非字节复制
  els.qualityBifNote.hidden = !(!!page.prep && fmt.startsWith('ventana-bif') && bf);
  // Unic TMAP06：重叠瓦片按绝对坐标拼接后重编码——「保留画质」是高质量
  // 重编码而非无损/原 JPEG 字节
  els.qualityTmapNote.hidden = !(!!page.prep && fmt.startsWith('unic-tmap06') && bf);
  // TMAP06 标定/关联图提醒：头部像素尺寸未核验 → 不写 µm/px；关联图不嵌入
  els.tmapCalibrationNote.hidden = !(!!page.prep && fmt.startsWith('unic-tmap06'));
  // F2（Aperio JPEG 2000 33003/33005）：没有字节搬运路径，两种画质都是
  // 解码后重编码（有损）
  els.qualityJp2kNote.hidden = !(!!page.prep && fmt.startsWith('aperio-svs-jp2k') && bf);
}

/// F2：JPEG2000 源没有「原样搬运」——两种画质的选项描述都要换成重编码
/// 语义（preserve = 逐 tile 解码后高质量重编码 q96 4:2:2；compact = 固定
/// compact 参数）。非 JP2K 恢复默认文案。语言切换后由 rerenderForLang →
/// renderQualitySection 重放（applyLang 按 data-i18n 重设默认文本，本函数
/// 在其之后运行覆盖 JP2K 的情况）。
function applyQualityOptionDescriptions() {
  const o = E.qualityOptionDescOverride(probeDoc());
  if (!els.qualityPreserveDesc || !els.qualityCompactDesc) return;
  els.qualityPreserveDesc.textContent = t(o ? o.preserve : 'tools.quality.preserve.desc');
  els.qualityCompactDesc.textContent = t(o ? o.compact : 'tools.quality.compact.desc');
}

/// 「像素严格无损」与「更小文件（有损）」互斥（U2 要求在 UI 也阻止，核心
/// 另有类型化拒绝兜底）：选了 compact → 禁用 strict；选了 strict → 禁用
/// compact。已经勾选的一方在切换时被自动改回（onEncodingChange /
/// onPolicyChange），并显示一次性说明。
function updateQualityPolicyGate() {
  const compactRadio = document.getElementById('quality-compact');
  const strictRadio = document.getElementById('policy-strict');
  if (!compactRadio || !strictRadio) return;
  const compactSel = selectedEncodingProfile() === E.ENCODING_PROFILES.COMPACT;
  const strictSel = selectedPolicy() === 'strict-lossless';
  compactRadio.disabled = strictSel || !!page.encodingLockedProfile;
  strictRadio.disabled = compactSel;
  const conflicted = compactSel && strictSel;
  els.qualityConflict.hidden = !conflicted;
  if (conflicted) els.qualityConflict.textContent = t('tools.quality.strict.conflict');
}

document.querySelectorAll('input[name="policy"]').forEach((r) => {
  r.addEventListener('change', () => {
    updateStrictWarning();
    // strict 与 compact 互斥：strict 被选中时把 compact 改回保留画质。
    // 派发 change 让 onEncodingChange 把改回的选择写回 prepared 记录
    //（radio 的程序化勾选本身不触发事件）。
    if (selectedPolicy() === 'strict-lossless'
        && selectedEncodingProfile() === E.ENCODING_PROFILES.COMPACT) {
      const preserve = document.getElementById('quality-preserve');
      if (preserve) {
        preserve.checked = true;
        preserve.dispatchEvent(new Event('change', { bubbles: true }));
        return;
      }
    }
    updateQualityPolicyGate();
    renderEstimate();
  });
});

/// 准备后改选明场输出格式：立即写回 prepared 记录（刷新后从任务列表「开始」
/// 仍带上该选择）。已开始的任务 radio 已禁用（锁定），不会到达这里；万一
/// 到达（运行器侧同样只允许 prepared），错误照常展示且记录不变。
async function onOutputFormatChange(ev) {
  if (!page.prep || page.outputLockedProfile || els.formatSection.hidden) return;
  try {
    await page.runner.setPreparedOutputProfile(page.prep.jobId, ev.target.value);
    refreshJobs();
  } catch (e) {
    showError(e);
  }
  renderOutputSummary();
}

document.querySelectorAll('input[name="outputFormat"]').forEach((r) => {
  r.addEventListener('change', onOutputFormatChange);
});

/// 准备后改选画质：立即写回 prepared 记录（与输出格式同一合同）；磁盘预估
/// 跟随所选编码取对应上界。strict+compact 在此也被阻止（选 compact 时把
/// strict 改回允许边缘重编码）。
async function onEncodingChange(ev) {
  if (!page.prep || page.encodingLockedProfile || els.qualityFieldset.hidden) return;
  if (ev.target.value === E.ENCODING_PROFILES.COMPACT
      && selectedPolicy() === 'strict-lossless') {
    const allow = document.getElementById('policy-allow-edge');
    if (allow) allow.checked = true;
  }
  updateStrictWarning();
  updateQualityPolicyGate();
  renderEstimate();
  try {
    await page.runner.setPreparedEncodingProfile(page.prep.jobId, ev.target.value);
    refreshJobs();
  } catch (e) {
    showError(e);
  }
}

document.querySelectorAll('input[name="encoding"]').forEach((r) => {
  r.addEventListener('change', onEncodingChange);
});

// -------------------------------------------------------------- convert --

const PHASE_LABEL_KEY = {
  selected: 'tools.phase.selected',
  probing: 'tools.phase.probing',
  planned: 'tools.phase.planned',
  running: 'tools.phase.running',
  paused: 'tools.phase.paused',
  finalizing: 'tools.phase.finalizing',
  validating: 'tools.phase.validating',
  ready: 'tools.phase.ready',
  exported: 'tools.phase.exported',
  failed: 'tools.phase.failed',
  cancelled: 'tools.phase.cancelled',
  cleanup_pending: 'tools.phase.cleanup_pending',
};

function phaseText(state) {
  const key = PHASE_LABEL_KEY[state];
  page.busyPhaseLabelKey = key;
  return key ? t(key) : state;
}

/// 摘要里当前准备的源名（与任务行同一优先顺序：文件夹名 → 入口文件名）。
function prepDisplayName() {
  return page.bundleFolderName
    || (page.prep && page.prep.identity && page.prep.identity.name)
    || (page.file && page.file.name)
    || (page.prep && page.prep.jobId) || null;
}

/// 转换卡上的任务名（任务列表开始/续跑时来自该行；手动开始来自当前摘要）。
/// 让「正在转换」的到底是哪个任务一眼可见，而不是借用上一张摘要卡。
function renderRunJob() {
  if (!els.runJob) return;
  if (!page.runJobName || !page.runJobState) {
    els.runJob.hidden = true;
    els.runJob.textContent = '';
    return;
  }
  els.runJob.hidden = false;
  els.runJob.textContent = t(`tools.run.job.${page.runJobState}`,
    { name: page.runJobName });
}

function setRunJob(name, state) {
  page.runJobName = name || null;
  page.runJobState = state || null;
  renderRunJob();
}

/// 转换失败的结果形态（含 worker 崩溃的 crashed:true）：能取到 error 就用
/// 类型化文案，否则给可读的通用失败文案——绝不把 undefined 塞进错误面板。
function showRunFailure(result) {
  const err = (result && result.error) || null;
  if (err && (err.code || err.message)) {
    showError(err);
    return;
  }
  showErrorText(t('tools.run.failed.unknown'));
}

/// 取消后的统一收尾（P2-5）：隐藏摘要与错误，只显示中性的「已取消」；
/// 完整包任务提示重新选择文件夹。不显示任何内部清理 token。
function showCancelledState(job = null) {
  const runName = page.runJobName
    || (job && job.source && (job.source.folderName || job.source.name)) || null;
  const wasBundle = !!page.bundleFiles
    || !!(job && job.source && job.source.folderName)
    || (!page.file && !!page.prep);
  resetFlowPanels();
  els.runSection.hidden = false;
  els.runStatus.textContent = phaseText('cancelled');
  els.runProgress.hidden = true;
  els.runBytes.hidden = true;
  els.cancelBtn.hidden = true;
  els.cancelBtn.disabled = false;
  setRunJob(runName, runName ? 'cancelled' : null);
  setPageMsg(wasBundle ? 'tools.run.cancelled.bundle.note' : 'tools.run.cancelled.note');
}

/// 转换执行驱动（手动开始与任务列表开始/续跑共用）：startJob → 进度/结果
/// UI。返回 worker 的 result（{ok}|{type:'cancelled'}|失败形态）。
/// F3：完整包任务 page.file 为 null——源已整包复制进 OPFS（bundle 记录），
/// startJob(null, {jobId}) 直接用暂存成员，无需（也无法）再提供 File。
async function driveConversion() {
  if (page.busy) return null;   // 同一标签页只允许一个转换（P1-4 / P2-12）
  if (!page.prep || (!page.file && !page.bundleFiles)) return null;
  clearError();
  setPageMsg(null);
  els.cancelBtn.hidden = false;
  els.cancelBtn.disabled = false;
  els.runProgress.hidden = false;
  els.runBytes.hidden = false;
  els.runProgress.classList.remove('progress-done');
  setProgress(els.runProgress, els.runBar, els.runBytes, 0, '');
  els.runStatus.textContent = phaseText('planned');
  setBeforeunload(true);
  page.running = true;
  setPageBusy(true);
  const jobName = prepDisplayName();
  setRunJob(jobName, 'running');
  try {
    // 开始即用当前 UI 选择（明场；荧光不传 → fl-ome / preserve；完整包
    // 恒为明场——荧光 MRXS 在准备时已被核心拒绝）；
    // 任务从此锁定该输出格式与画质。
    const outputProfile = outputProfileForModality(probeDoc().modality);
    const encodingProfile = encodingProfileForModality(probeDoc().modality);
    const { jobId, done } = await page.runner.startJob(page.file, {
      jobId: page.prep.jobId,
      profileId: selectedProfileId(),
      policy: selectedPolicy(),
      channelJson: page.channelJson,
      outputProfile,
      encodingProfile,
    });
    lockOutputProfile(outputProfile || E.defaultOutputProfile(probeDoc().modality));
    lockEncodingProfile(encodingProfile || E.defaultEncodingProfile());
    page.prep.jobId = jobId;
    // 记录已进入 planned/active：任务行立刻显示「转换中（本标签页）」
    refreshJobs();
    const result = await done;
    page.running = false;
    setBeforeunload(false);
    els.cancelBtn.hidden = true;
    if (result && result.ok) {
      els.runStatus.textContent = phaseText('ready');
      // NEW-a：成功必须落在 100%，且不因 180ms 过渡在截图/复核时停在中间值
      setProgress(els.runProgress, els.runBar, els.runBytes, 1, '');
      els.runProgress.classList.add('progress-done');
      setRunBytes('tools.run.done.bytes', { bytes: fmtBytes(result.result.output_bytes) });
      page.readyInfo = {
        jobId,
        outputBytes: result.result.output_bytes,
        sha256: result.validation && result.validation.sha256,
        modality: probeDoc().modality,
        outputProfile: result.result.output_profile || null,
        encoding: result.result.encoding || null,
        result: { format: result.result.format || null },
        sourceName: (page.prep.identity && page.prep.identity.name)
          || (page.file && page.file.name),
        channels: result.result.channels || [],
        composed: result.result.composed || null,
      };
      renderResultPanel();
      renderSaveStatus();
      setRunJob(jobName, 'done');
    } else if (isCancelledResult(result)) {
      showCancelledState();
    } else {
      els.runStatus.textContent = phaseText('failed');
      els.runProgress.hidden = true;
      els.runBytes.hidden = true;
      setRunJob(jobName, 'failed');
      showRunFailure(result);
    }
    setPageBusy(false);
    refreshJobs();
    return result;
  } catch (e) {
    page.running = false;
    setBeforeunload(false);
    els.cancelBtn.hidden = true;
    els.runStatus.textContent = phaseText('failed');
    els.runProgress.hidden = true;
    els.runBytes.hidden = true;
    // job_busy_this_tab 在任务真正开始前就拒绝了：不要把名字标成失败
    setRunJob(errCode(e) === 'job_busy_this_tab' ? null : jobName,
      errCode(e) === 'job_busy_this_tab' ? null : 'failed');
    showError(e);
    setPageBusy(false);
    refreshJobs();
    return { ok: false, error: e };
  }
}

async function onConvert() {
  await driveConversion();
}

async function onCancel() {
  // ≤250ms 反馈：cancelJob 同步翻转状态并终止 worker，随后清理任务目录。
  // 收尾（隐藏摘要/中性「已取消」）由 done 的取消分支统一完成；这里绝不
  // 展示 cleanup 的内部 token（P2-5）。
  els.runStatus.textContent = phaseText('cancelled');
  els.cancelBtn.disabled = true;
  try {
    await page.runner.cancelJob();
  } catch (e) {
    showError(e);
  } finally {
    els.cancelBtn.disabled = false;
    els.cancelBtn.hidden = true;
    els.runProgress.hidden = true;
    els.runBytes.hidden = true;
  }
  refreshJobs();
}

/// 取消准备（U2）：终止 worker、删除任务目录；runProbeFlow 的 probe promise
/// 以类型化 `cancelled` 拒绝（runner 小改动）并显示「已取消准备」。
async function onPrepareCancel() {
  els.prepareCancelBtn.disabled = true;
  try {
    await page.runner.cancelJob();
  } finally {
    els.prepareCancelBtn.disabled = false;
    els.prepareCancelBtn.hidden = true;
  }
}

// ---------------------------------------------------------------- ready --

function suggestedOutputName() {
  if (!page.readyInfo) return 'output.tif';
  return E.outputFileName(page.readyInfo.sourceName, page.readyInfo);
}

function renderResultPanel() {
  if (!page.readyInfo) { els.resultSection.hidden = true; return; }
  els.resultSection.hidden = false;
  const g = els.resultGrid;
  g.textContent = '';
  dlRow(g, t('tools.result.format'),
    t(`tools.result.format.${E.jobOutputProfile(page.readyInfo)}`), 'result-format');
  dlRow(g, t('tools.result.encoding'),
    encodingLabel(page.readyInfo.encoding || E.ENCODING_PROFILES.PRESERVE), 'result-encoding');
  dlRow(g, t('tools.result.size'), fmtBytes(page.readyInfo.outputBytes), 'result-size');
  dlRow(g, t('tools.result.sha256'), String(page.readyInfo.sha256 || '—'), 'result-sha');
  // F3（MRXS）：核心的拼接重编码摘要（合成/填充/去重 tile 计数）
  if (page.readyInfo.composed) {
    const c = page.readyInfo.composed;
    dlRow(g, t('tools.result.composed'),
      t('tools.result.composed.note', {
        composed: Number(c.tiles_composed || 0),
        filled: Number(c.tiles_filled || 0),
        deduped: Number(c.tiles_deduped || 0),
      }), 'result-composed');
  }
  if (page.readyInfo.modality === 'fluorescence') {
    (page.readyInfo.channels || []).forEach((c, i) => {
      const dw = Array.isArray(c.display_window) ? c.display_window : null;
      dlRow(g, t('tools.result.channel', { name: c.name }),
        dw ? t('tools.result.channel.window', { lower: dw[0], upper: dw[1] })
          : t('tools.result.channel.window.none'),
        `result-channel-${i}`);
    });
  }
}

/// 任务列表里的上传/继续/取消旧上传：反馈都写在结果面板（#upload-status）
/// 里——先把面板切到该任务并显示出来（刷新后面板本是隐藏的）。只用该任务
/// 自己的记录填面板，别的任务的上传状态不会带进来。
async function selectResultJob(jobId) {
  let job = (page.lastJobs || []).find((j) => j.id === jobId) || null;
  if (!job || !job.result) job = await page.runner.getJob(jobId).catch(() => null);
  if (!job || !job.result || !['ready', 'exported'].includes(job.state)) return;
  if (!page.readyInfo || page.readyInfo.jobId !== jobId) {
    page.readyInfo = {
      jobId,
      outputBytes: job.result.outputBytes,
      sha256: job.result.sha256,
      modality: job.modality,
      outputProfile: job.outputProfile || null,
      encoding: (job.result && job.result.encoding) || job.encodingProfile || null,
      result: { format: job.result.format || null },
      sourceName: job.source && job.source.name,
      channels: job.result.channels || [],
      composed: (job.result && job.result.composed) || null,
    };
    page.saveMsg = page.missingArtifacts && page.missingArtifacts.has(jobId)
      ? { key: 'tools.err.artifact_missing' } : null;
  }
  renderResultPanel();
  renderSaveStatus();
  if (typeof els.resultSection.scrollIntoView === 'function') {
    els.resultSection.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
}

function renderSaveStatus() {
  const supported = typeof window.showSaveFilePicker === 'function';
  els.saveCapabilityNote.hidden = supported;
  if (!supported) paintSaveUnsupportedNote();   // 文字 + 「打开工作台」链接
  else if (els.saveCapabilityNote.textContent) els.saveCapabilityNote.textContent = '';
  const artifactGone = !!(page.readyInfo && page.missingArtifacts
    && page.missingArtifacts.has(page.readyInfo.jobId));
  // P2-10：产物已被浏览器清理的任务不再提供「保存」
  els.saveBtn.disabled = !supported || !page.readyInfo || artifactGone;
  els.saveBtn.setAttribute('aria-disabled', String(els.saveBtn.disabled));
  // 「保存到我的项目」独立于本机保存能力（匿名可用，登录在点击后要求）；
  // 产物被清理的任务同样不提供
  const inlineOff = !inlineUploadEnabled();
  els.saveProjectBtn.disabled = !page.readyInfo || artifactGone || inlineOff;
  els.saveProjectBtn.setAttribute('aria-disabled', String(els.saveProjectBtn.disabled));
  if (inlineOff) {
    els.saveProjectBtn.title = t('tools.saveproj.inline.disabled');
  } else if (els.saveProjectBtn.title) {
    els.saveProjectBtn.title = '';
  }
  if (!supported) {
    els.saveStatus.textContent = t('tools.result.save.unsupported');
    return;
  }
  if (page.saveMsg) {
    els.saveStatus.textContent = t(page.saveMsg.key, page.saveMsg.vars || {});
  } else if (els.saveStatus.dataset.busy !== '1') {
    els.saveStatus.textContent = '';
  }
}

/// 记录「该任务的产物已被浏览器清理」：失败后任务行不再显示「保存」，
/// 结果面板的保存按钮同时禁用（P2-10）。
function markArtifactMissing(jobId) {
  if (!jobId) return;
  if (!page.missingArtifacts) page.missingArtifacts = new Set();
  page.missingArtifacts.add(jobId);
}

async function onSave(job = null) {
  if (typeof window.showSaveFilePicker !== 'function') return;
  const jobId = job ? job.id : page.readyInfo && page.readyInfo.jobId;
  if (!jobId) return;
  clearError();
  els.saveBtn.disabled = true;
  els.saveStatus.dataset.busy = '1';
  page.saveMsg = null;
  els.saveStatus.textContent = t('tools.result.save.working');
  try {
    // 必须在用户手势内请求 picker（Chromium 手势约束）
    const handle = await window.showSaveFilePicker({
      suggestedName: job ? E.outputFileName(job.source && job.source.name, job) : suggestedOutputName(),
      types: E.saveFileTypes(job || page.readyInfo),
    });
    const r = await page.runner.exportJob(jobId, () => handle.createWritable());
    page.saveMsg = { key: 'tools.result.save.done', vars: { bytes: fmtBytes(r.exportedBytes) } };
    els.saveStatus.textContent = t('tools.result.save.done', { bytes: fmtBytes(r.exportedBytes) });
    setPageMsg(null);
  } catch (e) {
    if (e && e.name === 'AbortError') {
      page.saveMsg = { key: 'tools.result.save.aborted' };
    } else if (errCode(e) === 'artifact_missing') {
      markArtifactMissing(jobId);
      page.saveMsg = { key: 'tools.err.artifact_missing' };
      showError(e);
    } else {
      page.saveMsg = { key: 'tools.result.save.failed' };
      showError(e);
    }
  } finally {
    delete els.saveStatus.dataset.busy;
    renderSaveStatus();
    refreshJobs();
  }
}

// ------------------------------------------------- 保存到我的项目（移交） --
//
// 本页不建第二套上传引擎（2026-10-22 设计）：确认目标后写 runner intent
//（channel "workbench-inline" + 账号/目标，与工作台 restore 的期待一致），
// 再跳转 /app?save-job=<jobId>。工作台恢复行默认停放（设计 §6.2），只对
// 这一个显式移交的 jobId 自动续传一次（用户在本页的点击即同意），随后
// 摘除 URL 标记；发布后的文件夹关联复用 importAssociateUploaded。

async function fetchAuthInfo() {
  const r = await fetch('/api/auth/info', {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  });
  // 401/403 = 服务端明确「未登录」（部署对匿名 /api/* 直接拒）→ 归一成
  // 匿名结果，调用方给登录深链接；其余非 2xx 才是真正的获取失败
  if (r.status === 401 || r.status === 403) {
    return { auth_enabled: true, user_id: null, username: null, anonymous: true };
  }
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

/// 工作台「面板内上传」是否开启（服务端随本页 body 属性下发同一开关；
/// 旧模板缓存无此属性 → 未知不预判，交给工作台的显式失败提示）
function inlineUploadEnabled() {
  const v = document.body && document.body.dataset
    ? document.body.dataset.inlineConvertEnabled
    : undefined;
  if (v === undefined || v === '') return true;
  return v === 'true';
}

// 待保存标记（复核 r2 #1）：登录/注册绕出本页时记录 {jobId, at}，工作台
// 登录后据此提示「继续保存」。24h 过期；写 intent 成功、对话框取消、任务
// 消失、?save= 回跳都会清除。
const PENDING_SAVE_KEY = 'pt.tools.pendingSave.v1';
const PENDING_SAVE_TTL = 24 * 60 * 60 * 1000;

function pendingSaveWrite(jobId) {
  try {
    window.localStorage.setItem(PENDING_SAVE_KEY, JSON.stringify({
      jobId: String(jobId || ''), at: Date.now(),
    }));
  } catch { /* 隐私模式等：标记只是体验增强 */ }
}

function pendingSaveClear() {
  try { window.localStorage.removeItem(PENDING_SAVE_KEY); } catch { /* */ }
}

let saveLoginJobId = null;

/// 未登录：小对话框三选（登录 / 注册 / 取消），无自动跳转计时器。
function showLoginHandoff(jobId) {
  saveLoginJobId = String(jobId || '');
  els.saveLoginDialog.showModal();
  els.saveLoginCancelBtn.focus();
}

function onSaveLoginDialogClose() {
  // 关闭（取消/Esc）：不记录标记，留在本页（结果卡仍在，可另存到电脑）
  saveLoginJobId = null;
}

function onSaveLoginFormSubmit(ev) {
  ev.preventDefault();
  const submitter = ev.submitter;
  const value = (submitter && submitter.value) || 'cancel';
  if (value === 'cancel' || !saveLoginJobId) {
    els.saveLoginDialog.close('cancel');
    return;
  }
  pendingSaveWrite(saveLoginJobId);
  const jobId = encodeURIComponent(saveLoginJobId);
  saveLoginJobId = null;
  // 登录带 next 原样回跳；注册不带 next（注册流不读该参数）——回来后由
  // 工作台的待保存标记提示「继续保存」
  window.location.href = value === 'login'
    ? '/login?next=' + encodeURIComponent('/tools/slides?save=' + jobId)
    : '/register';
}

/// 「保存到我的项目」唯一入口（结果卡与任务行共用）。身份只在点击时查；
/// 未登录 → 登录/注册/取消对话框（待保存标记在工作台提示继续保存）。
async function saveToProject(jobId) {
  if (!jobId) return;
  clearError();
  let info = null;
  try {
    info = await fetchAuthInfo();
  } catch (e) {
    // 对话框尚未打开：状态写在页面上（saveProjectStatus 在隐藏的对话框里，
    // 写在那里用户什么都看不到）
    setPageMsg('tools.saveproj.load.failed', { e: String(e && e.message || e) });
    return;
  }
  // 免认证部署（auth_enabled=false）无登录语义：直接走移交（账号空串，
  // 与工作台免认证口径一致）
  if (info && info.auth_enabled && !info.user_id) {
    showLoginHandoff(jobId);
    return;
  }
  await openSaveProjectDialog(jobId, info || {});
}

/// 工具页自己的账号/文件夹读取（仅对话框打开期间使用）
async function fetchProjects() {
  const r = await fetch('/api/projects', {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

/// 保存对话框状态（一次只服务于一个 job）
const saveProject = {
  jobId: null,
  identity: null,     // { username, user_id, auth_enabled }
  job: null,          // runner.listJobs() 里的一行
  ready: false,       // 字段就绪（目标可选）
};

function setSaveProjectMsg(key, vars) {
  if (key === null) {
    els.saveProjectStatus.textContent = '';
    return;
  }
  els.saveProjectStatus.textContent = t(key, vars || {});
}

function setSaveProjectError(key, vars) {
  if (key === null) {
    els.saveProjectError.hidden = true;
    els.saveProjectError.textContent = '';
    return;
  }
  els.saveProjectError.hidden = false;
  els.saveProjectError.textContent = t(key, vars || {});
}

async function openSaveProjectDialog(jobId, identity) {
  saveProject.jobId = jobId;
  saveProject.identity = identity || {};
  saveProject.job = null;
  saveProject.ready = false;
  els.saveProjectFields.hidden = true;
  setSaveProjectError(null);
  setSaveProjectMsg('tools.saveproj.loading');
  els.saveProjectConfirmBtn.disabled = true;
  els.saveProjectDialog.showModal();
  els.saveProjectCancelBtn.focus();
  try {
    // 任务与产物：任务缺失 / 已被清理都在这里显式说明（?save= 回跳路径）
    const jobs = await page.runner.listJobs();
    const job = (jobs || []).find((j) => j.id === jobId) || null;
    if (!job || !job.result ||
        !['ready', 'exported'].includes(job.state)) {
      pendingSaveClear();   // 任务已消失：待保存标记收口
      setSaveProjectMsg(null);
      setSaveProjectError('tools.saveproj.job.missing');
      return;
    }
    saveProject.job = job;
    try {
      const view = await page.runner.artifactView(jobId);
      if (!view) throw new Error('missing');
    } catch (e) {
      pendingSaveClear();   // 产物已被清理：待保存标记收口
      setSaveProjectMsg(null);
      setSaveProjectError('tools.saveproj.artifact.missing');
      saveProject.job = null;
      return;
    }
    // 账号行
    els.saveProjectAccount.textContent = String(saveProject.identity.username || '');
    // 文件行：产物名 + 大小（与「保存到电脑」同一口径；产物真实文件名在
    // 工作台上传时按任务记录推导，这里先给源名）
    const size = Number(job.result.outputBytes) || 0;
    els.saveProjectFile.textContent =
      String((job.source && job.source.name) || jobId)
      + ' · ' + fmtBytes(size);
    await renderSaveProjectDestinations();
  } catch (e) {
    setSaveProjectMsg(null);
    setSaveProjectError('tools.saveproj.load.failed', { e: String(e && e.message || e) });
    return;
  }
}

/// 目标下拉：根（未归类）+ 可写、未归档的文件夹（层级缩进）+ 新建文件夹…
async function renderSaveProjectDestinations() {
  let projects = [];
  try {
    projects = await fetchProjects();
  } catch (e) {
    setSaveProjectMsg(null);
    setSaveProjectError('tools.saveproj.projects.failed',
      { e: String(e && e.message || e) });
    return;
  }
  const usable = (projects || []).filter((p) => p && p.can_write && !p.archived);
  const byParent = new Map();
  const roots = [];
  for (const p of usable) {
    const parent = p.parent_project_id || null;
    if (byParent.has(parent)) byParent.get(parent).push(p);
    else byParent.set(parent, [p]);
  }
  const rows = [];
  const walk = (parent, depth) => {
    for (const p of (byParent.get(parent) || [])) {
      rows.push({ p, depth });
      walk(p.pid, depth + 1);
    }
  };
  walk(null, 0);
  els.saveProjectDest.innerHTML = '';
  const rootOpt = document.createElement('option');
  rootOpt.value = '';
  rootOpt.textContent = t('tools.saveproj.unfiled');
  els.saveProjectDest.appendChild(rootOpt);
  for (const { p, depth } of rows) {
    const opt = document.createElement('option');
    opt.value = p.pid;
    opt.textContent = '　'.repeat(depth)
      + (depth > 0 ? '↳ ' : '') + (p.name || p.pid);
    els.saveProjectDest.appendChild(opt);
  }
  const newOpt = document.createElement('option');
  newOpt.value = 'new';
  newOpt.textContent = t('tools.saveproj.new');
  els.saveProjectDest.appendChild(newOpt);
  els.saveProjectDest.value = '';
  onSaveProjectDestChange();
  els.saveProjectFields.hidden = false;
  setSaveProjectMsg(null);
  saveProject.ready = true;
  els.saveProjectConfirmBtn.disabled = false;
  els.saveProjectConfirmBtn.focus();
}

function onSaveProjectDestChange() {
  const isNew = els.saveProjectDest.value === 'new';
  els.saveProjectNewRow.hidden = !isNew;
  if (isNew) els.saveProjectNewName.focus();
}

/// 确认：目标快照与工作台 importTargetSnapshot 同形（pid / 新文件夹名 +
/// createKey 幂等键）；intent 账号取此刻登录身份（工作台换账号 → 既有
/// expected_account/归属复核 409 保护，绝不静默上传）。
async function onConfirmSaveProject() {
  if (!saveProject.ready || !saveProject.job) return;
  if (els.saveProjectDest.value === 'new') {
    const name = (els.saveProjectNewName.value || '').trim();
    if (!name) {
      setSaveProjectError('tools.saveproj.new.required');
      return;
    }
  }
  const snap = {
    pid: els.saveProjectDest.value === 'new' ? '' : (els.saveProjectDest.value || ''),
    newProjectName: els.saveProjectDest.value === 'new'
      ? (els.saveProjectNewName.value || '').trim() : '',
    createdPid: '',
    createKey: (crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : 'spj-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10),
  };
  els.saveProjectConfirmBtn.disabled = true;
  setSaveProjectMsg('tools.saveproj.handoff');
  try {
    await page.runner.setJobIntent(saveProject.jobId, {
      channel: 'workbench-inline',
      state: 'pending',
      account: String(saveProject.identity.user_id || ''),
      accountLabel: String(saveProject.identity.username || ''),
      target: snap,
      via: 'converter-save',
    });
    pendingSaveClear();   // 移交已写入：待保存标记收口
  } catch (e) {
    els.saveProjectConfirmBtn.disabled = false;
    setSaveProjectMsg(null);
    setSaveProjectError('tools.saveproj.intent.failed', { e: String(e && e.message || e) });
    return;
  }
  window.location.href = '/app?save-job=' + encodeURIComponent(saveProject.jobId);
}

function onSaveProjectDialogClose() {
  // 关闭（取消/Esc）：什么都不写、不跳转；待保存标记一并清除
  saveProject.ready = false;
  pendingSaveClear();
}

/// 表单提交（method=dialog 的两个 submit 按钮）：拦截后手动分发——
/// 取消 → 立即关闭（什么都不写）；保存 → 对话框保持打开（「正在移交…」），
/// intent 写成功后整页跳 /app?save-job=<jobId>，失败则留在对话框内报错。
function onSaveProjectFormSubmit(ev) {
  ev.preventDefault();
  const submitter = ev.submitter;   // SubmitEvent.submitter（两个提交按钮）
  const value = (submitter && submitter.value) || 'cancel';
  if (value !== 'confirm') {
    els.saveProjectDialog.close('cancel');
    return;
  }
  void onConfirmSaveProject();
}

/// ?save=<jobId>（登录回跳）：摘参数 → 找任务 → 就绪且产物在 → 自动打开
/// 保存对话框；缺失/被清理都显式说明（匿名回跳时给登录说明）。
async function handleSaveParam() {
  let m = null;
  try { m = window.location.search.match(/[?&]save=([^&]+)/); } catch { return; }
  pendingSaveClear();   // 已回到本页：待保存标记收口（改由 ?save= 驱动）
  if (!m) return;
  const jobId = decodeURIComponent(m[1] || '');
  try {
    const u = new URL(window.location.href);
    u.searchParams.delete('save');
    window.history.replaceState({}, '', u.pathname + u.search + u.hash);
  } catch { /* 保留参数不致命 */ }
  if (!jobId) return;
  let info = null;
  try {
    info = await fetchAuthInfo();
  } catch (e) {
    setPageMsg('tools.saveproj.load.failed', { e: String(e && e.message || e) });
    return;
  }
  if (info && info.auth_enabled && !info.user_id) {
    // 仍未登录（会话在回跳途中丢失等）：与首次点击同一对话框
    showLoginHandoff(jobId);
    return;
  }
  await openSaveProjectDialog(jobId, info || {});
}

// ------------------------------------------------------------- job list --

const STATE_TONE = {
  ready: 'ok', exported: 'ok',
  failed: 'err', cancelled: 'err', cleanup_pending: 'warn',
  running: 'run', validating: 'run', finalizing: 'run', paused: 'warn',
  staging: 'warn', prepared: 'ok', planned: 'run', probing: 'run',
};

function stateLabel(state) {
  const key = `tools.jobs.state.${state}`;
  const s = t(key);
  return s === key ? state : s;
}

const JOB_ACTION_LABEL_KEY = {
  start: 'tools.jobs.action.start',
  resume: 'tools.jobs.action.resume',
  export: 'tools.jobs.action.export',
  wait: 'tools.jobs.action.wait',
  discard: 'tools.jobs.action.discard',
};

function renderJobs(jobs) {
  page.lastJobs = jobs;
  // 任务记录默认折叠；有任务（可续跑/可保存/上传中）时自动展开一次
  if (jobs.length && !els.jobsDetails.open) els.jobsDetails.open = true;
  els.jobsList.textContent = '';
  if (!jobs.length) {
    const p = document.createElement('p');
    p.className = 'jobs-empty';
    p.textContent = t('tools.jobs.empty');
    els.jobsList.appendChild(p);
    applyBusyState();
    return;
  }
  for (const job of jobs) {
    const row = document.createElement('article');
    row.className = 'job-row';
    row.dataset.jobId = job.id;
    row.dataset.nextAction = job.nextAction;

    // 2026-10-21 视觉改版：桌面三列网格（名称/摘要 · 状态 · 操作），
    // 窄屏时名称摘要独占一行；行之间用细分隔线，不再使用独立边框卡片。
    const main = document.createElement('div');
    main.className = 'job-main';
    const name = document.createElement('p');
    name.className = 'job-name';
    // F3：完整包任务显示所选文件夹名（记录里保存了 folderName；散文件/
    // 旧记录回退到入口文件名）
    name.textContent = (job.source && (job.source.folderName || job.source.name)) || job.id;

    const meta = document.createElement('p');
    meta.className = 'job-meta';
    const size = job.source ? fmtBytes(job.source.size) : '—';
    // committed progress only means something for a run that can continue
    meta.textContent = (job.nextAction === 'resume' || job.nextAction === 'wait')
      ? t('tools.jobs.meta', { size, committed: fmtBytes(job.committedBytes || 0) })
      : t('tools.jobs.meta.source', { size });
    if (job.result && job.result.outputBytes) {
      meta.textContent += ` · ${t('tools.jobs.result.size', { bytes: fmtBytes(job.result.outputBytes) })}`;
    }
    if (job.settings && job.settings.profileId) {
      meta.textContent += ` · ${t('tools.jobs.settings', {
        profile: t(`tools.profile.${job.settings.profileId}`),
        policy: t(`tools.jobs.policy.${job.settings.policy}`),
      })}`;
    }
    // 输出格式（prepared 起记录里就有；含旧任务回退出的 classic/fl-ome）
    if (job.outputProfile) {
      meta.textContent += ` · ${t('tools.jobs.format', {
        name: t(`tools.jobs.format.${job.outputProfile}`),
      })}`;
    }
    // 画质：只在非默认（更小文件/有损）时展示，避免给旧任务刷屏
    if (job.encodingProfile === E.ENCODING_PROFILES.COMPACT) {
      meta.textContent += ` · ${t('tools.jobs.encoding', {
        name: t('tools.jobs.encoding.compact'),
      })}`;
    }
    if (job.hasChannelJson) meta.textContent += ` · ${t('tools.jobs.channel')}`;
    main.appendChild(name);
    main.appendChild(meta);

    const st = document.createElement('span');
    st.className = 'job-state';
    // the record says planned/paused while this tab's worker is converting
    const shownState = job.active ? 'running' : job.state;
    st.dataset.tone = STATE_TONE[shownState] || '';
    st.textContent = stateLabel(shownState);
    const statusCell = document.createElement('div');
    statusCell.className = 'job-status-cell';
    statusCell.appendChild(st);

    const actions = document.createElement('div');
    actions.className = 'job-actions';
    const primary = job.nextAction;
    const artifactGone = !!(page.missingArtifacts && page.missingArtifacts.has(job.id));
    // 2026-10-20：历史 pending 上传意图一律不再执行——开始/续跑只是本地
    // 转换（普通文案），完成后停在结果面板，绝不自动上传。
    // P3-17：运行中的行（nextAction 'wait'）不是可点击动作——渲染为状态文本。
    if (primary === 'wait') {
      const statusEl = document.createElement('span');
      statusEl.className = 'job-action-status';
      statusEl.setAttribute('role', 'status');
      statusEl.dataset.action = 'wait';
      statusEl.textContent = t('tools.jobs.action.wait');
      actions.appendChild(statusEl);
    } else if (primary === 'export' && artifactGone) {
      // P2-10：产物已被浏览器清理——不再提供「保存」，明确要求删除后重转
      const statusEl = document.createElement('span');
      statusEl.className = 'job-action-status err';
      statusEl.setAttribute('role', 'status');
      statusEl.dataset.action = 'export-missing';
      statusEl.textContent = t('tools.jobs.artifact_missing');
      actions.appendChild(statusEl);
    } else if (primary !== 'discard') {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = primary === 'export' ? 'btn btn-primary' : 'btn btn-secondary';
      btn.dataset.action = primary;
      btn.textContent = t(JOB_ACTION_LABEL_KEY[primary] || primary);
      // disabled 状态在 renderJobs 末尾由 applyBusyState 统一刷新（忙碌时
      // 开始/续跑不可点，P1-4）
      btn.disabled = primary === 'export' && typeof window.showSaveFilePicker !== 'function';
      btn.addEventListener('click', () => onJobAction(primary, job));
      actions.appendChild(btn);
      if (primary === 'export' && inlineUploadEnabled()) {
        // 「保存到我的项目」：与「保存」同排的第二个出口（移交工作台上传；
        // 面板内上传关闭时不渲染——工作台无法承接）
        const sp = document.createElement('button');
        sp.type = 'button';
        sp.className = 'btn btn-secondary';
        sp.dataset.action = 'saveProject';
        sp.textContent = t('tools.result.saveProject');
        sp.addEventListener('click', () => { void saveToProject(job.id); });
        actions.appendChild(sp);
      }
    }
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn btn-danger';
    del.dataset.action = 'discard';
    // 运行中的任务：删除会先停止转换（runner 侧 cancel-first），标签写明
    del.textContent = primary === 'wait'
      ? t('tools.jobs.action.stop_discard') : t('tools.jobs.action.discard');
    del.setAttribute('data-job-discard', job.id);
    del.addEventListener('click', (ev) => onJobAction('discard', job, ev.currentTarget));
    actions.appendChild(del);
    row.appendChild(main);
    row.appendChild(statusCell);
    row.appendChild(actions);
    els.jobsList.appendChild(row);
  }
  applyBusyState();
}

async function onJobAction(action, job, btnEl = null) {
  clearError();
  try {
    if (action === 'discard') {
      if (!window.confirm(t('tools.jobs.discard.confirm'))) return;
      const wasRunning = job.nextAction === 'wait' || !!job.active;
      // P2-7：删除运行中的任务会先停止转换——立刻给出「正在停止…」反馈
      if (wasRunning) {
        setPageMsg('tools.jobs.stopping');
        if (btnEl) {
          btnEl.disabled = true;
          btnEl.textContent = t('tools.jobs.action.stopping');
        }
      }
      try {
        await page.runner.discardJob(job.id);
      } catch (e) {
        const info = (e && e.error) || {};
        // 遗留上传记录（没有任何标签在传）：再确认一次放弃上传才删除。
        // 2026-10-20：本页不再联网取消旧任务——记录就地放弃，服务器对长
        // 时间无活动的任务有到期清理兜底（其他标签/工作台正在传的不受影响：
        // lockHeld=true 时这里不会到达）。
        if (errCode(e) !== 'upload_active' || info.lockHeld !== false) throw e;
        if (!window.confirm(t('tools.upload.abandon.confirm'))) return;
        await page.runner.discardJob(job.id, { abandonUpload: true });
      }
      // 运行中的任务由 done 的取消分支留下「已取消…」说明；普通删除清掉
      // 临时状态。两者都不显示清理内部 token。
      if (!wasRunning) setPageMsg(null);
      refreshJobs();
      return;
    }
    if (action === 'export') {
      if (typeof window.showSaveFilePicker !== 'function') return;
      // Select the row's result and invoke the picker in this user gesture;
      // do not wait for any asynchronous job lookup before opening it.
      const selected = selectResultJob(job.id);
      await onSave(job);
      await selected;
      return;
    }
    if (action === 'start' || action === 'resume') {
      // 同一标签页一次只跑一个转换（P1-4 / P2-12）：忙碌时任务列表的
      // 开始/续跑也被禁用，这里补齐程序化触发的防护。
      if (page.busy) {
        setPageMsg('tools.err.job_busy_this_tab');
        return;
      }
      // prepared/paused 任务：源副本已在浏览器临时存储，无需重选文件。
      // resume 不传设置（含输出格式/画质）→ 沿用任务记录；start（prepared）
      // 只有当可见的输出格式/画质组属于该任务（本会话刚准备、未锁定）时才
      // 传当前界面选择，否则（刷新后 / 设置组属于别的任务）不传 → 运行器用
      // 任务记录里保存的值——记录值优先于隐藏的 radio 默认值。channel.json
      // 一律沿用准备时保存在任务记录里的那份。两种动作开始后格式与画质都
      // 锁定为任务实际值。
      // 任务列表开始/续跑的目标可能不是当前摘要里的任务（刷新后或先准备了
      // 另一个文件）。此时摘要/准备卡描述的是**另一个**任务——继续显示会
      // 让用户误以为它正在转换，也会让「开始转换」看起来可用。隐藏并清掉
      // 该摘要；目标任务仍留在任务列表里（完成后不会复活这张摘要卡）。
      const thisPrep = page.prep && page.prep.jobId === job.id;
      const jobName = (job.source && (job.source.folderName || job.source.name)) || job.id;
      if (!thisPrep) {
        els.stageSection.hidden = true;
        els.summarySection.hidden = true;
        page.prep = null;
        page.file = null;
        page.bundleFiles = null;
        page.bundleFolderName = null;
        page.prepareContext = null;
        page.channelJson = null;
        page.channelJsonName = null;
        page.outputLockedProfile = null;
        page.encodingLockedProfile = null;
      }
      clearError();
      setPageMsg(null);
      els.runSection.hidden = false;
      els.cancelBtn.hidden = false;
      els.cancelBtn.disabled = false;
      els.runProgress.hidden = false;
      els.runBytes.hidden = false;
      els.runProgress.classList.remove('progress-done');
      setProgress(els.runProgress, els.runBar, els.runBytes, 0, '');
      els.runStatus.textContent = phaseText(action === 'resume' ? 'paused' : 'planned');
      setBeforeunload(true);
      page.running = true;
      setRunJob(jobName, 'running');
      setPageBusy(true);
      try {
        const startOutputProfile = (action === 'start' && thisPrep && !els.formatSection.hidden)
          ? outputProfileForModality(job.modality) : undefined;
        const startEncodingProfile = (action === 'start' && thisPrep && !els.qualityFieldset.hidden)
          ? encodingProfileForModality(job.modality) : undefined;
        const started = action === 'resume'
          ? await page.runner.resumeJob(job.id)
          : await page.runner.startJob(null, {
            jobId: job.id,
            profileId: selectedProfileId(),
            policy: selectedPolicy(),
            outputProfile: startOutputProfile,
            encodingProfile: startEncodingProfile,
          });
        // 只锁定当前摘要所属任务的画质/格式；另一个任务的摘要已被隐藏，
        // 绝不能把本任务的设置写到它上面（审计：synthetic.vms 显示
        // “本任务画质已确定”而实际在转 vms-big40）。
        if (thisPrep) {
          lockOutputProfile(startOutputProfile || job.outputProfile
            || E.defaultOutputProfile(job.modality));
          lockEncodingProfile(startEncodingProfile
            || (job.encodingProfile && E.encodingFitsModality(job.encodingProfile, job.modality)
              ? job.encodingProfile : null)
            || E.defaultEncodingProfile());
        }
        // 记录已进入 planned/active：任务行立刻显示「转换中（本标签页）」
        refreshJobs();
        const result = await started.done;
        page.running = false;
        setBeforeunload(false);
        els.cancelBtn.hidden = true;
        if (result && result.ok) {
          els.runStatus.textContent = phaseText('ready');
          // NEW-a：任务列表开始的转换同样把进度条落在 100%
          setProgress(els.runProgress, els.runBar, els.runBytes, 1, '');
          els.runProgress.classList.add('progress-done');
          setRunBytes('tools.run.done.bytes', { bytes: fmtBytes(result.result.output_bytes) });
          page.readyInfo = {
            jobId: job.id,
            outputBytes: result.result.output_bytes,
            sha256: result.validation && result.validation.sha256,
            modality: job.modality,
            outputProfile: result.result.output_profile || job.outputProfile || null,
            encoding: result.result.encoding || job.encodingProfile || null,
            result: { format: result.result.format || null },
            sourceName: job.source && job.source.name,
            channels: result.result.channels || [],
            composed: result.result.composed || null,
          };
          page.saveMsg = null;
          renderResultPanel();
          renderSaveStatus();
          setRunJob(jobName, 'done');
          // 2026-10-20：历史 pending 上传意图不再执行——转换完成后停在结果
          // 面板（保存到电脑），绝不自动上传。
        } else if (isCancelledResult(result)) {
          showCancelledState(job);
        } else {
          els.runStatus.textContent = phaseText('failed');
          els.runProgress.hidden = true;
          els.runBytes.hidden = true;
          setRunJob(jobName, 'failed');
          showRunFailure(result);   // 含 worker crashed:true 的通用可读文案
        }
        setPageBusy(false);
        refreshJobs();
      } catch (e) {
        // job_busy_this_tab 等类型化拒绝：当前运行中的任务不受影响
        page.running = false;
        setBeforeunload(false);
        els.cancelBtn.hidden = true;
        els.runProgress.hidden = true;
        els.runBytes.hidden = true;
        setRunJob(errCode(e) === 'job_busy_this_tab' ? null : jobName,
          errCode(e) === 'job_busy_this_tab' ? null : 'failed');
        setPageBusy(false);
        showError(e);
        refreshJobs();
      }
    }
  } catch (e) {
    showError(e);
    setBeforeunload(false);
    els.cancelBtn.hidden = true;
    els.runProgress.hidden = true;
    els.runBytes.hidden = true;
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.textContent = t('tools.jobs.action.discard');
    }
    refreshJobs();
  }
}

async function refreshJobs() {
  try {
    const jobs = await page.runner.listJobs();
    renderJobs(jobs);
  } catch (e) {
    console.warn('listJobs failed', e);
  }
}

// --------------------------------------------------------- beforeunload --

function setBeforeunload(on) {
  page.beforeunloadOn = on;
}

window.addEventListener('beforeunload', (ev) => {
  if (page.beforeunloadOn) {
    ev.preventDefault();
    ev.returnValue = '';
  }
});

// ---------------------------------------------------------------- init --

async function init() {
  document.title = t('tools.doc.title');
  renderSaveStatus();
  try {
    page.runner = await SlideToolsRunner.create();
  } catch (e) {
    setPageMsg('tools.init.failed');
    showError(e);
    return;
  }
  page.runner.on('progress', onProgress);
  page.runner.on('state', onRunnerState);
  refreshJobs();

  // 拖放：全页阻止默认导航（拖到页面任意位置都不会打开/替换文档），
  // 拖放区内的 drop 进入与 file input 相同的 prepareSource 流程
  for (const type of ['dragover', 'drop']) {
    window.addEventListener(type, (ev) => { ev.preventDefault(); });
  }
  els.dropZone.addEventListener('dragover', () => {
    if (!page.busy) els.dropZone.classList.add('dragover');
  });
  // P3-13：dragenter/dragleave 计数 + relatedTarget —— 在子元素之间移动时
  // 不会因为子元素冒泡的 dragleave 把高亮闪掉。
  let dragDepth = 0;
  els.dropZone.addEventListener('dragenter', (ev) => {
    ev.preventDefault();
    dragDepth += 1;
    if (!page.busy) els.dropZone.classList.add('dragover');
  });
  els.dropZone.addEventListener('dragleave', (ev) => {
    dragDepth = Math.max(0, dragDepth - 1);
    const toChild = !!(ev.relatedTarget && els.dropZone.contains(ev.relatedTarget));
    if (dragDepth === 0 || !toChild) {
      dragDepth = 0;
      els.dropZone.classList.remove('dragover');
    }
  });
  els.dropZone.addEventListener('drop', (ev) => {
    dragDepth = 0;
    els.dropZone.classList.remove('dragover');
    handleDropData(ev.dataTransfer);
  });
  // 外框只接收拖放；点击选择由各自按钮处理，避免整框反馈误导文件夹选择。
  els.pickFileBtn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    // P1-2：打开选择器前清空 value，同一文件再次选择仍触发 change
    els.fileInput.value = '';
    els.fileInput.click();
  });
  els.fileInput.addEventListener('change', () => { onFilePicked(); });
  // F3：MRXS 完整包——webkitdirectory 文件夹选择（键盘/触屏可达，input
  // 视觉隐藏 + tabindex="-1"（按钮是唯一入口）；change 后交给 prepareBundleSource
  els.pickFolderBtn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    els.folderInput.value = '';
    els.folderInput.click();
  });
  els.folderInput.addEventListener('change', () => { onFolderPicked(); });
  // MRXS 同名数据文件夹协助：主按钮开目录选择器（缺 API 回退 webkitdirectory
  // input）；取消回到干净输入态
  els.companionPickBtn.addEventListener('click', () => { void onCompanionPick(); });
  els.companionCancelBtn.addEventListener('click', () => { onCompanionCancel(); });
  els.channelInput.addEventListener('change', () => { onChannelPicked(); });
  els.convertBtn.addEventListener('click', () => { onConvert(); });
  els.cancelBtn.addEventListener('click', () => { onCancel(); });
  els.prepareCancelBtn.addEventListener('click', () => { onPrepareCancel(); });
  els.saveBtn.addEventListener('click', () => { onSave(); });
  // 保存到我的项目：身份只在点击时查（匿名用户给登录深链接）；确认后移交
  els.saveProjectBtn.addEventListener('click', () => {
    const jobId = page.readyInfo && page.readyInfo.jobId;
    void saveToProject(jobId);
  });
  els.saveProjectDest.addEventListener('change', () => { onSaveProjectDestChange(); });
  els.saveProjectForm.addEventListener('submit', onSaveProjectFormSubmit);
  els.saveProjectDialog.addEventListener('close', () => { onSaveProjectDialogClose(); });
  els.saveLoginForm.addEventListener('submit', onSaveLoginFormSubmit);
  els.saveLoginDialog.addEventListener('close', () => { onSaveLoginDialogClose(); });
  // ?save=<jobId>（登录回跳）：runner 就绪后处理（摘参数/找任务/开对话框）
  void handleSaveParam();
  // 保存能力缺失：禁用新转换入口 + 常驻工作台指引（renderSaveStatus 已把
  // 指引写进 #save-capability-note；既有任务的查看/删除不受影响）
  applyBusyState();
  // 测试/诊断可观测钩子（不承载任何逻辑）
  window.__stToolsReady = true;
}

const runBytesMsg = { key: null, vars: null };
function setRunBytes(key, vars) {
  runBytesMsg.key = key;
  runBytesMsg.vars = vars;
  els.runBytes.textContent = t(key, vars);
}

function onProgress(p) {
  if (p.unit === 'stage') {
    const frac = p.total ? p.done / p.total : 0;
    els.stageSection.hidden = false;
    setProgress(els.stageProgress, els.stageBar, els.stageBytes, frac,
      `${fmtBytes(p.done)} / ${fmtBytes(p.total)}`);
    return;
  }
  if (p.unit === 'stage-bundle') {
    // F3：成员逐个复制（单个复用缓冲），进度按字节聚合；顺带显示第几个成员
    const frac = p.total ? p.done / p.total : 0;
    els.stageSection.hidden = false;
    setProgress(els.stageProgress, els.stageBar, els.stageBytes, frac,
      t('tools.stage.bundle.bytes', {
        done: fmtBytes(p.done), total: fmtBytes(p.total),
        member: p.member, members: p.members,
      }));
    return;
  }
  if (p.unit === 'level') {
    const frac = p.total ? p.done / p.total : 0;
    setProgress(els.runProgress, els.runBar, els.runBytes, frac, '');
    setRunBytes('tools.run.progress.level', {
      done: p.done, total: p.total,
      bytes: fmtBytes(p.committed_bytes || 0),
    });
  } else if (p.unit === 'tile-row') {
    setRunBytes('tools.run.progress.row', {
      level: p.level, done: p.done, total: p.total,
      bytes: fmtBytes(p.committed_bytes || 0),
    });
  }
}

function onRunnerState(s) {
  if (!s || !s.to) return;
  if (['running', 'finalizing', 'validating', 'probing', 'planned'].includes(s.to)) {
    els.runStatus.textContent = phaseText(s.to);
  }
  // NEW-a：runner 报 ready 时进度条立即落 100%（done 的收尾还会再确认一次）
  if (s.to === 'ready') {
    setProgress(els.runProgress, els.runBar, null, 1, null);
    els.runProgress.classList.add('progress-done');
  }
  if (s.to === 'running' || s.to === 'finalizing' || s.to === 'validating') {
    refreshJobs();
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
