/**
 * U2 页面简化（/tools/slides）的纯 helper 与源码级 wiring 约定：
 *
 *  - 入口统一：file input 与 drop 都进入同一 prepareSource 流程；一次只接受
 *    一个切片文件（多个普通文件 → 明确提示）；.mrxs/.dat（单个/散装）与
 *    文件夹/目录 drop 进入同一 prepareBundleSource → runner.prepareBundle
 *    （缺成员 → 类型化信息，任何复制之前拒绝，绝不静默丢弃）；
 *  - 扩展名只是提示（engine.INPUT_EXTENSION_HINTS），识别靠文件头魔数表
 *    （engine.SUPPORTED_MAGICS / magicSupported）——两者同在一处维护；
 *  - 画质（U3 编码档）与输出格式同一合同：prepared 可改并落盘、开始即锁定、
 *    重开以任务记录为准、荧光不提供有损模式；MRXS 的画质说明（拼接后重编码）
 *    只在 MRXS 识别后出现；
 *  - strict 无损与 compact 互斥必须在 UI 阻止（核心另有类型化拒绝兜底）；
 *  - 磁盘预估跟随所选编码取上界；
 *  - 上传阶段文案与计划对齐（排队 → 上传至腾讯云 → 工作台接收 → 校验/发布
 *    → 可查看），zh/en 成对。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
// eslint-disable-next-line
import * as E from "../../static/tools/slide-transform/engine.js";
// eslint-disable-next-line
import {
	bundleRoute, bundleRows, collectEntryFiles, folderNameFromRelPath,
	looksLikeBundleMember,
} from "../../static/tools/tools-slides-bundle.js";

const here = dirname(fileURLToPath(import.meta.url));
const read = (rel: string) => readFileSync(resolve(here, "../..", rel), "utf8");
const shellSrc = read("templates/tools_slides.html");
const pageSrc = read("static/tools/tools-slides.js");
const cssSrc = read("static/tools/tools-slides.css");
const i18nSrc = read("static/i18n.js");
const runnerSrc = read("static/tools/slide-transform/runner.js");

describe("input extension hints (engine.js, hint only — never identification)", () => {
	it("bundle inputs (.mrxs/.dat) are hinted as bundles", () => {
		expect(E.inputExtensionHint("slide.mrxs")).toBe("bundle");
		expect(E.inputExtensionHint("SLIDE.MRXS")).toBe("bundle");
		expect(E.inputExtensionHint("index.dat")).toBe("bundle");
	});

	it("SVS gets no extension hint: the TIFF header + bounded sniff identify it", () => {
		expect(E.inputExtensionHint("scan.svs")).toBeNull();
		expect(E.inputExtensionHint("SCAN.SVS")).toBeNull();
		expect(pageSrc).not.toMatch(/hint === 'svs'/);
	});

	it("supported and unknown extensions get no hint (header decides)", () => {
		expect(E.inputExtensionHint("a.kfb")).toBeNull();
		expect(E.inputExtensionHint("b.kfbf")).toBeNull();
		expect(E.inputExtensionHint("c.txt")).toBeNull();
		expect(E.inputExtensionHint("")).toBeNull();
	});

	it("hints and identification live in one place, next to the magic table", () => {
		const engineSrc = read("static/tools/slide-transform/engine.js");
		const magicsPos = engineSrc.indexOf("SUPPORTED_MAGICS");
		const hintsPos = engineSrc.indexOf("INPUT_EXTENSION_HINTS");
		expect(magicsPos).toBeGreaterThan(-1);
		expect(hintsPos).toBeGreaterThan(magicsPos);
		expect(hintsPos - magicsPos).toBeLessThan(3000);
	});
});

describe("U2 default view (template structure)", () => {
	it("drop zone + pick button + hidden-but-focusable file input, one accept list", () => {
		expect(shellSrc).toContain('id="drop-zone"');
		expect(shellSrc).toContain('id="pick-file-btn"');
		expect(shellSrc).toMatch(/<input id="file-input" name="file" type="file" accept="\.kfb,\.kfbf,\.svs,\.scn,\.ndpi,\.bif,\.tmap,\.TMAP,\.ome\.tif,\.ome\.tiff,\.tif,\.tiff,\.vms,\.vmu,\.mrxs,\.dat,\.bmp,\.jpg,\.jpeg" class="visually-hidden-input"/);
		expect(cssSrc).toContain(".visually-hidden-input");
		expect(cssSrc).not.toMatch(/#file-input\s*\{[^}]*display:\s*none/);
		expect(cssSrc).toContain(".drop-zone.dragover");
	});

	it("F3: secondary folder action + hidden-but-focusable webkitdirectory input", () => {
		expect(shellSrc).toContain('id="pick-folder-btn"');
		expect(shellSrc).toContain('data-i18n="tools.drop.pick.folder"');
		expect(shellSrc).toMatch(/<input id="folder-input" name="folder" type="file" webkitdirectory multiple class="visually-hidden-input"/);
		// keyboard/touch reachable: same visually-hidden pattern (not display:none)
		expect(cssSrc).not.toMatch(/#folder-input\s*\{[^}]*display:\s*none/);
		// both pick buttons stopPropagation so the drop-zone click keeps the
		// single-file default; the folder route goes through its own input
		expect(pageSrc).toMatch(/pickFolderBtn\.addEventListener\('click', \(ev\) => \{[\s\S]*?ev\.stopPropagation\(\);[\s\S]*?folderInput\.click\(\)/);
		expect(pageSrc).toContain("els.folderInput.addEventListener('change', () => { onFolderPicked(); })");
	});

	it("F3/F6: one-line MRXS/NDPI quality note under the radios, format-specific", () => {
		const fieldsetEnd = shellSrc.indexOf("</fieldset>", shellSrc.indexOf('id="quality-fieldset"'));
		const noteAt = shellSrc.indexOf('id="quality-mrxs-note"');
		expect(noteAt).toBeGreaterThan(fieldsetEnd);
		expect(shellSrc).toContain('data-i18n="tools.quality.mrxs.note"');
		// F6: NDPI 的分段解码重编码说明行（同样常重编码语义）
		const ndpiAt = shellSrc.indexOf('id="quality-ndpi-note"');
		expect(ndpiAt).toBeGreaterThan(fieldsetEnd);
		expect(shellSrc).toContain('data-i18n="tools.quality.ndpi.note"');
		expect(pageSrc).toMatch(/const fmt = String\(probeDoc\(\)\.format \|\| ''\)/);
		expect(pageSrc).toMatch(/fmt\.startsWith\('mirax'\) && bf\)/);
		expect(pageSrc).toContain("els.qualityMrsxNote.hidden = !(!!page.prep && fmt.startsWith('mirax') && bf)");
		expect(pageSrc).toMatch(/els\.qualityNdpiNote\.hidden = !\(!!page\.prep && fmt\.startsWith\('hamamatsu-ndpi'\) && bf\)/);
		// reset hides them again (a later single-file pick must not keep a note)
		expect(pageSrc).toMatch(/resetFlowPanels\(\)[\s\S]*?els\.qualityMrsxNote\.hidden = true/);
		expect(pageSrc).toMatch(/resetFlowPanels\(\)[\s\S]*?els\.qualityNdpiNote\.hidden = true/);
	});

	it("one-line local explanation with details folded (no upload on this page)", () => {
		expect(shellSrc).toContain('id="privacy-details"');
		expect(shellSrc).toContain('data-i18n="tools.privacy.brief"');
		expect(shellSrc).toContain('data-i18n="tools.privacy.details"');
	});

	it("distinct prepare and convert stages, preparation cancellable", () => {
		expect(shellSrc).toContain('id="stage-section"');
		expect(shellSrc).toContain('id="prepare-cancel-btn"');
		expect(shellSrc).toContain('id="run-section"');
		expect(pageSrc).toContain("async function onPrepareCancel()");
		expect(pageSrc).toContain("els.prepareCancelBtn.addEventListener('click'");
	});

	it("config summary card with output summary, quality and one action", () => {
		expect(shellSrc).toContain('id="summary-section"');
		expect(shellSrc).toContain('id="summary-grid"');
		expect(shellSrc).toContain('id="output-summary-text"');
		expect(shellSrc).not.toContain('data-i18n="tools.run.upload"');
		expect(shellSrc).toContain('id="convert-btn"');
		expect(shellSrc).toContain('id="more-options"');
		expect(pageSrc).toContain("renderSummary()");
	});

	it("advanced items are collapsed inside 更多选项; the disk confirm is not", () => {
		const more = shellSrc.slice(shellSrc.indexOf('id="more-options"'));
		const moreEnd = more.indexOf("</details>");
		const inner = more.slice(0, moreEnd);
		for (const id of ["channel-section", "format-section", "profile-section",
			"policy-section", "probe-section", "estimate-section"]) {
			expect(inner).toContain(`id="${id}"`);
		}
		// the space confirmation is a modal dialog outside any collapsible
		// (2026-10-20: account-dialog 随上传控制器退役移除，disk-dialog 是唯一对话框)
		const dialog = shellSrc.slice(shellSrc.indexOf("<dialog"));
		expect(dialog).toContain('id="disk-dialog"');
		expect(dialog).not.toContain("account-dialog");
		expect(dialog).not.toContain("<details");
	});

	it("job records are a collapsed details card that auto-opens with jobs", () => {
		expect(shellSrc).toMatch(/<details class="card" id="jobs-details">/);
		expect(pageSrc).toMatch(/if \(jobs\.length && !els\.jobsDetails\.open\) els\.jobsDetails\.open = true;/);
	});

	it("explicit 保存到电脑, never claiming the OPFS artifact is saved", () => {
		expect(shellSrc).toContain('data-i18n="tools.result.save"');
		expect(shellSrc).toMatch(/data-i18n="tools\.result\.not\.saved\.warn"[^<]*>/);
	});
});

describe("U2 entry flow (page source wiring)", () => {
	it("file input and drop converge on one prepareSource flow", () => {
		// 2026-10：opts 携带 drop 事件捕获的文件句柄（MRXS 协助的 startIn 定位）
		expect(pageSrc).toContain("async function prepareSource(fileList, opts = {})");
		expect(pageSrc).toContain("els.fileInput.addEventListener('change', () => { onFilePicked(); })");
		// onFilePicked is the shared next step of the manual and dropped path
		expect(pageSrc).toMatch(/async function prepareSource\(fileList, opts = \{\}\)[\s\S]*?await onFilePicked\(route\.file\)/);
		expect(pageSrc).toMatch(/async function onFilePicked\(explicitFile\)[\s\S]*?await runProbeFlow\(\)/);
		// 2026-10-20：工作台交接接收器已随上传移除
		expect(pageSrc).not.toContain("takeHandoffFile");
	});

	it("drop navigation is prevented anywhere on the page", () => {
		expect(pageSrc).toMatch(/for \(const type of \['dragover', 'drop'\]\)[\s\S]*?ev\.preventDefault\(\)/);
		expect(pageSrc).toContain("handleDropData(ev.dataTransfer)");
	});

	it("multiple files: visible message, nothing silently dropped or started", () => {
		expect(pageSrc).toMatch(/route\.route === 'multiple'[\s\S]*?tools\.drop\.multiple/);
		expect(pageSrc).toMatch(/onFilePicked\(explicitFile\)[\s\S]*?list\.length > 1[\s\S]*?tools\.drop\.multiple/);
	});

	it("a drop while converting is deflected with a message, not a panel reset", () => {
		// P1-4: the whole page is busy during preparation and conversion; the
		// drop path must show the busy message instead of resetFlowPanels
		expect(pageSrc).toMatch(/if \(page\.busy\) \{[\s\S]*?tools\.drop\.busy/);
		expect(i18nSrc).toContain('"tools.drop.busy"');
		expect(i18nSrc).toContain('"tools.drop.busy": "A conversion is running');
	});

	it(".mrxs/.dat inputs (single or loose) go through the planner — typed missing-members, never silent", () => {
		// drop route: bundleRoute sends any .mrxs/.dat member to prepareBundleSource
		//（2026-10：纯散 .mrxs 在此之前被同名文件夹协助截停——见
		// mrxs-companion.test.ts；文件夹/目录输入（fromFolder）与装配后的
		// 整包仍直达 planner）
		expect(pageSrc).toMatch(/route\.route === 'bundle'[\s\S]*?await prepareBundleSource\(route\.files, \{ fileHandles: opts\.fileHandles \}\)/);
		expect(pageSrc).toMatch(/if \(!opts\.fromFolder && !page\.companion\) \{[\s\S]*?C\.detectNeedsFolder\(rows\)/);
		// file-input route (a .mrxs picked manually) has the same planner delegation
		expect(pageSrc).toMatch(/inputExtensionHint\(file\.name\) === 'bundle'[\s\S]*?await prepareBundleSource\(\[file\]\)/);
		// the planner (engine.planBundle 按入口类型分派 via runner.prepareBundle) refuses
		// incomplete bundles BEFORE any copy; the page shows its typed message
		expect(runnerSrc).toMatch(/const plan = await E\.planBundle\(files\)/);
		expect(i18nSrc).toMatch(/"tools\.drop\.bundle": "[^"]*完整包[^"]*"/);
	});

	it("directory drops are traversed in the handler and fed to the same bundle flow", () => {
		expect(pageSrc).toMatch(/entry\.isDirectory[\s\S]*?dirEntries\.push\(entry\)/);
		expect(pageSrc).toMatch(/onDirectoryDropped\(dirEntries\)/);
		expect(pageSrc).toMatch(/collectEntryFiles\(entry, \{ maxMembers: E\.MRXS_MAX_MEMBERS \}\)/);
		// untraversable directory → message pointing at the folder button
		expect(pageSrc).toMatch(/catch \(e\) \{[\s\S]*?tools\.drop\.dir\.unreadable/);
		expect(i18nSrc).toContain('"tools.drop.dir.unreadable"');
		expect(i18nSrc).toMatch(/"tools\.drop\.dir\.unreadable": "[^"]*「选择文件夹」[^"]*"/);
		expect(i18nSrc).toMatch(/"tools\.drop\.dir\.unreadable": "[^"]*“Choose folder”[^"]*"/);
	});

	it("cancelled preparation is a typed state, not an error panel", () => {
		expect(pageSrc).toMatch(/code === 'cancelled'[\s\S]*?tools\.stage\.cancelled/);
		// the runner rejects pending prepare requests as cancelled on cancel
		expect(runnerSrc).toContain("_rejectPending('cancelled', E.ERROR_CODES.CANCELLED)");
		expect(runnerSrc).toContain("_rejectPending(reason, code = E.ERROR_CODES.IO_RECOVERABLE)");
	});
});

describe("U2 quality wiring (page source, mirrors the output-profile contract)", () => {
	it("template offers preserve (default) and compact with the plan wording", () => {
		expect(shellSrc).toContain('id="quality-preserve" name="encoding" value="preserve-source-v1" checked');
		expect(shellSrc).toContain('id="quality-compact" name="encoding" value="compact-jpeg-v1"');
		expect(shellSrc).toContain('data-i18n="tools.quality.preserve"');
		expect(shellSrc).toContain('data-i18n="tools.quality.compact"');
	});

	it("prepare passes only a brightfield quality choice; fluorescence stays undefined", () => {
		expect(pageSrc)
			.toContain("encodingProfile: sniffedModality === 'brightfield' ? selectedEncodingProfile() : undefined");
		expect(pageSrc).toMatch(/function encodingProfileForModality\(modality\)[\s\S]*?modality === 'brightfield' \? selectedEncodingProfile\(\) : undefined/);
	});

	it("fresh start passes the selection; list-start only for this job's visible group", () => {
		expect(pageSrc).toMatch(/startJob\(page\.file, \{[\s\S]*?encodingProfile,/);
		expect(pageSrc).toMatch(/startJob\(null, \{[\s\S]*?encodingProfile: startEncodingProfile,/);
		expect(pageSrc).toContain("(action === 'start' && thisPrep && !els.qualityFieldset.hidden)");
		expect(pageSrc).toContain("await page.runner.resumeJob(job.id)");
	});

	it("a radio change after prepare is persisted into the prepared record", () => {
		expect(pageSrc).toContain("setPreparedEncodingProfile(page.prep.jobId, ev.target.value)");
		expect(pageSrc).toMatch(/input\[name="encoding"\][\s\S]*?onEncodingChange/);
		expect(pageSrc).toMatch(/onEncodingChange\(ev\)[\s\S]*?page\.encodingLockedProfile \|\| els\.qualityFieldset\.hidden/);
	});

	it("a started job locks the choice and shows its actual quality", () => {
		expect(pageSrc).toContain("lockEncodingProfile(encodingProfile || E.defaultEncodingProfile())");
		expect(pageSrc).toContain("els.qualityFieldset.disabled = !!locked;");
		expect(pageSrc).toContain("t('tools.quality.locked'");
		expect(pageSrc).toMatch(/t\('tools\.result\.encoding'\)[\s\S]*?encodingLabel\(/);
	});

	it("strict-lossless and compact are mutually exclusive in the UI as well", () => {
		expect(pageSrc).toMatch(/compactRadio\.disabled = strictSel/);
		expect(pageSrc).toMatch(/strictRadio\.disabled = compactSel/);
		expect(pageSrc).toMatch(/selectedPolicy\(\) === 'strict-lossless'[\s\S]*?quality-preserve[\s\S]*?checked = true/);
		expect(pageSrc).toMatch(/ev\.target\.value === E\.ENCODING_PROFILES\.COMPACT[\s\S]*?policy-allow-edge[\s\S]*?checked = true/);
	});

	it("the disk estimate follows the chosen encoding", () => {
		expect(pageSrc).toMatch(/diskNeedBytes\(est, \{[\s\S]*?encoding: selectedEncodingProfile\(\)/);
		expect(pageSrc).toMatch(/onEncodingChange\(ev\)[\s\S]*?renderEstimate\(\)/);
	});

	it("job rows call out the non-default (compact) encoding only", () => {
		expect(pageSrc).toMatch(/job\.encodingProfile === E\.ENCODING_PROFILES\.COMPACT[\s\S]*?tools\.jobs\.encoding/);
	});
});

describe("U2 i18n (zh/en pairs for the new concepts)", () => {
	it("quality labels carry the exact plan meanings", () => {
		expect(i18nSrc).toContain('"tools.quality.preserve": "保留画质（推荐）"');
		expect(i18nSrc).toContain('"tools.quality.preserve.desc": "尽量保留源切片的原有编码：完整 tile 原样搬运，边缘不完整 tile 可能被重编码（该部分有损）；不是像素级无损。"');
		expect(i18nSrc).toContain('"tools.quality.compact": "更小文件（有损）"');
		expect(i18nSrc).toContain('"tools.quality.compact.desc": "每个 tile 解码后按固定参数重新编码（仅明场）：文件通常变小，但节省幅度可能有限；不适合颜色定量用途。"');
		expect(i18nSrc).toContain('"tools.quality.preserve": "Keep source quality (recommended)"');
		expect(i18nSrc).toContain('"tools.quality.compact": "Smaller file (lossy)"');
	});

	it("2026-10-20 local-only: no upload entries remain on the tool page", () => {
		// 上传已收敛到工作台：页面模板与页面脚本都不再有上传入口/引擎
		expect(shellSrc).not.toContain("cos-uploader.js");
		expect(shellSrc).not.toContain("convert-upload-btn");
		expect(shellSrc).not.toContain("upload-block");
		expect(shellSrc).not.toContain("direct-upload-btn");
		expect(shellSrc).toContain('data-i18n="tools.privacy.brief"');
		expect(shellSrc).toContain('data-i18n="tools.privacy.details"');
		expect(shellSrc).toContain('href="/app"');
		expect(shellSrc).toContain('data-i18n="tools.nav.workbench"');
		expect(pageSrc).not.toContain("createUploadController");
		expect(pageSrc).not.toContain("createDirectUploadController");
		expect(pageSrc).toContain("showDirectHint(file)");
		// 兼容格式提示（不上传，指引工作台）
		expect(i18nSrc).toContain('"tools.direct.hint": "此文件无需转换，可直接使用；如需上传，请前往工作台。"');
	});

	it("drop hints explain MRXS bundles (complete package + folder button) in both languages", () => {
		expect(i18nSrc).toContain('"tools.drop.multiple"');
		expect(i18nSrc).toContain('"tools.drop.bundle"');
		// the old “MRXS not supported yet” wording is gone
		expect(i18nSrc).not.toMatch(/当前版本尚未支持，未复制任何文件/);
		expect(i18nSrc).not.toMatch(/this version does not support yet — nothing was copied/);
		expect(i18nSrc).toMatch(/"tools\.drop\.bundle": "[^"]*完整包[^"]*Slidedat\.ini[^"]*"/);
		expect(i18nSrc).toMatch(/"tools\.drop\.bundle": "[^"]*complete bundle[^"]*Slidedat\.ini[^"]*"/);
		expect(i18nSrc).toContain('"tools.drop.bundle.multiple"');
		// Subtitle and detailed input hint include MRXS; rejection messages
		// point to that maintained list rather than duplicate its formats.
		// 2026-10-21 视觉改版：Hero 副标题收敛为一行；完整格式清单仍在
		// tools.input.file.hint（下一行断言），不丢失。
		expect(i18nSrc).toContain('"tools.subtitle": "在本机浏览器内完成病理切片格式转换，无需上传服务器"');
		expect(i18nSrc).toContain('"tools.subtitle": "Convert pathology slides entirely in your local browser, with no upload to a server"');
		expect(i18nSrc).toMatch(/"tools\.input\.file\.hint": "[^"]*(?:MRXS 完整包|VMS 完整包)[^"]*"/);
		expect(i18nSrc).toMatch(/"tools\.input\.folder\.label"[\s\S]*"tools\.input\.folder\.label"/);
		expect(i18nSrc).toMatch(/"tools\.err\.unsupported_input": "[^"]*未做任何复制[^"]*切片转换指南[^"]*"/);
		expect(i18nSrc).toMatch(/"tools\.err\.unsupported_input": "[^"]*nothing was copied[^"]*slide conversion guide[^"]*"/);
		// template defaults mirror the zh table (renderer works before i18n.js)
		const zhHint = i18nSrc.match(/"tools\.input\.file\.hint": "([^"]*)"/);
		expect(zhHint && shellSrc.includes(zhHint[1])).toBe(true);
		const zhNote = i18nSrc.match(/"tools\.quality\.mrxs\.note": "([^"]*)"/);
		expect(zhNote && shellSrc.includes(zhNote[1])).toBe(true);
	});
});

/// 假的 FileSystemDirectoryEntry/FileSystemFileEntry（readEntries 批量 ≤100、
/// 必须循环到空批；file() 回调风格）——驱动真实遍历逻辑。
function fakeFileEntry(name: string, bytes = 3) {
	return {
		isFile: true, isDirectory: false, name,
		file: (cb: (f: { name: string; size: number }) => void) => cb({ name, size: bytes }),
	};
}
function fakeDirEntry(name: string, children: any[], batchSize = 100) {
	return {
		isFile: false, isDirectory: true, name,
		createReader() {
			let i = 0;
			return {
				readEntries: (ok: (b: any[]) => void, err?: (e: unknown) => void) => {
					try { ok(children.slice(i, i + batchSize)); i += batchSize; }
					catch (e) { if (err) err(e); }
				},
			};
		},
	};
}

describe("bundle input routing (tools-slides-bundle.js, pure)", () => {
	it("any .mrxs/.dat member routes to the planner (bundle), never silently dropped", () => {
		expect(bundleRoute([]).route).toBe("empty");
		expect(bundleRoute([{ name: "a.kfb" }]).route).toBe("single");
		expect(bundleRoute([{ name: "a.kfb" }, { name: "b.kfb" }]).route).toBe("multiple");
		expect(bundleRoute([{ name: "CMU.mrxs" }]).route).toBe("bundle");
		expect(bundleRoute([{ name: "a.kfb" }, { name: "Index.dat" }]).route).toBe("bundle");
		expect(looksLikeBundleMember("SLIDE.MRXS")).toBe(true);
		expect(looksLikeBundleMember("Data0000.dat")).toBe(true);
		expect(looksLikeBundleMember("scan.VMS")).toBe(true);
		expect(looksLikeBundleMember("raw.vmu")).toBe(true);
		expect(looksLikeBundleMember("a.kfb")).toBe(false);
	});
	it("rows normalise File objects (webkitRelativePath) and {name, relPath, file} alike", () => {
		const picked = { name: "x.mrxs", webkitRelativePath: "top/x.mrxs", size: 1 };
		expect(bundleRows([picked])[0].relPath).toBe("top/x.mrxs");
		const traversed = { name: "x.mrxs", relPath: "top/x.mrxs", file: { size: 1 } };
		expect(bundleRows([traversed])[0].relPath).toBe("top/x.mrxs");
		expect(bundleRows([null, undefined])).toEqual([]);
	});
	it("folderNameFromRelPath: first segment only when there is a folder layer", () => {
		expect(folderNameFromRelPath("CMU-1/CMU-1.mrxs")).toBe("CMU-1");
		expect(folderNameFromRelPath("CMU-1/CMU-1/Slidedat.ini")).toBe("CMU-1");
		expect(folderNameFromRelPath("CMU.mrxs")).toBeNull();
		expect(folderNameFromRelPath("")).toBeNull();
	});
});

describe("directory drop traversal (tools-slides-bundle.js)", () => {
	it("walks nested folders and prefixes relative paths with the folder chain", async () => {
		const dropped = fakeDirEntry("CMU-1", [
			fakeFileEntry("CMU-1.mrxs"),
			fakeDirEntry("CMU-1", [
				fakeFileEntry("Slidedat.ini"),
				fakeFileEntry("Index.dat"),
				fakeDirEntry("empty", []),
			]),
		]);
		const rows = await collectEntryFiles(dropped);
		expect(rows.map((r: any) => r.relPath)).toEqual([
			"CMU-1/CMU-1.mrxs",
			"CMU-1/CMU-1/Slidedat.ini",
			"CMU-1/CMU-1/Index.dat",
		]);
		expect(rows[0].file).toEqual({ name: "CMU-1.mrxs", size: 3 });
	});
	it("loops readEntries until an empty batch (batches smaller than 100)", async () => {
		const files = Array.from({ length: 7 }, (_, i) => fakeFileEntry(`Data000${i}.dat`));
		const rows = await collectEntryFiles(fakeDirEntry("d", files, 2));
		expect(rows).toHaveLength(7);
	});
	it("bounds the member count (mirrors the engine cap)", async () => {
		const files = Array.from({ length: 6 }, (_, i) => fakeFileEntry(`f${i}.dat`));
		await expect(collectEntryFiles(fakeDirEntry("d", files), { maxMembers: 3 }))
			.rejects.toMatchObject({ code: "too_many_members" });
	});
	it("untraversable entries reject (the page then points at the folder button)", async () => {
		await expect(collectEntryFiles({ isDirectory: true, name: "d" } as any)).rejects.toBeInstanceOf(TypeError);
		await expect(collectEntryFiles({ isFile: true, name: "f" } as any)).rejects.toBeInstanceOf(TypeError);
	});
	it("a traversed drop forms a plan the engine accepts (root = dropped folder)", async () => {
		const SLIDEDAT = [
			"[GENERAL]", "SLIDE_ID = 0123456789ABCDEF0123456789ABCDEF",
			"SLIDE_TYPE = SLIDE_TYPE_BRIGHTFIELD",
			"[HIERARCHICAL]", "INDEXFILE = Index.dat", "HIER_COUNT = 1",
			"NONHIER_COUNT = 1", "HIER_0_NAME = Slide zoom level", "HIER_0_COUNT = 1",
			"NONHIER_0_NAME = Scan data layer", "NONHIER_0_COUNT = 1",
			"[DATAFILE]", "FILE_COUNT = 1", "FILE_0 = Data0000.dat",
		].join("\n");
		// 真实目录布局：根文件夹 CMU/ 内含 CMU.mrxs + 同名目录 CMU/
		// （file 需要最小 Blob 形状：planMrxBundle 直接 slice 读 Slidedat）
		const mk = (n: string, b: any = "x") => {
			const data = typeof b === "string" ? new TextEncoder().encode(b) : b;
			const rel = n === "CMU.mrxs" ? "CMU/CMU.mrxs" : `CMU/CMU/${n}`;
			return {
				name: n, relPath: rel,
				file: {
					name: n, size: data.length,
					slice: (a: number, e: number) => ({
						arrayBuffer: async () => data.slice(a, e).buffer,
					}),
				},
			};
		};
		const rows = [
			mk("CMU.mrxs", "entry"),
			mk("Slidedat.ini", SLIDEDAT),
			mk("Index.dat", "index"),
			mk("Data0000.dat", "aaaa"),
		];
		const plan = await E.planMrxBundle(rows as any, async (f: any) => f.file.bytes);
		expect(plan.stem).toBe("CMU");
		expect(plan.required).toEqual(["CMU.mrxs", "CMU/Slidedat.ini", "CMU/Index.dat", "CMU/Data0000.dat"]);
	});
});

describe("missing-member messaging (planner, before any copy)", () => {
	it("a lone .mrxs names the same-name folder members it still needs", async () => {
		const row = (n: string) => ({ name: n, relPath: `CMU/${n}`, file: { name: n, size: 1 } });
		await expect(E.planMrxBundle([row("CMU.mrxs")] as any))
			.rejects.toMatchObject({ error: { code: "unsupported_input", kind: "mrxs-bundle" } });
		let caught: any = null;
		try { await E.planMrxBundle([row("CMU.mrxs")] as any); } catch (e) { caught = e; }
		expect(caught.error.message).toContain("Slidedat.ini");
		expect(caught.error.message).toContain("完整包");
	});
	it("a lone .dat says the .mrxs entry is missing (no copy started)", async () => {
		const row = (n: string) => ({ name: n, relPath: `CMU/${n}`, file: { name: n, size: 1 } });
		let caught: any = null;
		try { await E.planMrxBundle([row("Data0000.dat")] as any); } catch (e) { caught = e; }
		expect(caught.error.code).toBe("unsupported_input");
		expect(caught.error.missing).toEqual(["<slide>.mrxs"]);
		expect(caught.error.message).toContain(".mrxs");
	});
	it("loose files without the folder layer cannot form a bundle (typed refusal, not silence)", async () => {
		// dropped loose files carry no folder prefix → member names don't match
		const SLIDEDAT = ["[HIERARCHICAL]", "INDEXFILE = Index.dat",
			"[DATAFILE]", "FILE_COUNT = 1", "FILE_0 = Data0000.dat"].join("\n");
		const files: any[] = [
			{ name: "CMU.mrxs", file: { name: "CMU.mrxs", size: 1 } },
			{ name: "Slidedat.ini", file: { name: "Slidedat.ini", size: SLIDEDAT.length } },
		];
		await expect(E.planMrxBundle(files)).rejects.toMatchObject({ error: { code: "unsupported_input" } });
	});
});

describe("bundle job list + result wiring (page source)", () => {
	it("job rows show the picked folder name for bundle jobs", () => {
		expect(pageSrc).toMatch(/job\.source && \(job\.source\.folderName \|\| job\.source\.name\)/);
		expect(runnerSrc).toMatch(/folderName: id0\.folderName \|\| null/);
		expect(runnerSrc).toMatch(/folderName: typeof opts\.folderName === 'string'/);
	});
	it("an interrupted copy (staging) never offers 开始", () => {
		// _summary: nextAction for 'staging' stays 'discard'; the sweep removes
		// the dir on next start (C2-proven), so the list cannot offer start
		expect(runnerSrc).toMatch(/else if \(state === 'prepared'\) nextAction = 'start';/);
		expect(runnerSrc).not.toMatch(/staging'\) nextAction = 'start'/);
		expect(runnerSrc).toContain("if (rec && rec.upload) continue;");
		expect(runnerSrc).toMatch(/if \(!rec \|\| rec\.state === 'staging'\) victims\.push\(name\)/);
	});
	it("the result panel shows the core's composed summary when present", () => {
		expect(shellSrc).toContain('id="result-grid"');
		expect(pageSrc).toMatch(/tools\.result\.composed[\s\S]*?result-composed/);
		expect(pageSrc).toMatch(/composed: result\.result\.composed \|\| null/);
		expect(pageSrc).toMatch(/composed: \(job\.result && job\.result\.composed\) \|\| null/);
		expect(runnerSrc).toMatch(/composed: rec\.result\.composed \|\| null/);
		expect(i18nSrc).toContain('"tools.result.composed"');
		expect(i18nSrc).toContain('"tools.result.composed.note"');
	});
	it("member-copy progress drives the same stage bar, by bytes", () => {
		expect(pageSrc).toMatch(/p\.unit === 'stage-bundle'[\s\S]*?tools\.stage\.bundle\.bytes/);
		expect(i18nSrc).toContain('"tools.stage.bundle.bytes"');
		expect(i18nSrc).toMatch(/"tools\.stage\.bundle\.bytes": "[^"]*\{done\}[^"]*\{member\}/);
	});
	it("bundle prepare shares the single-file stage/cancel/disk-confirm UI", () => {
		expect(pageSrc).toContain("async function prepareBundleSource(files, opts = {})");
		expect(pageSrc).toContain("await runBundleProbeFlow();");
		expect(pageSrc).toMatch(/runPrepareFlow\(async \(\) => prepareBundleWithDiskFlow\(rows/);
		expect(pageSrc).toMatch(/async function runPrepareFlow\(doPrepare, totalBytes\)/);
		// same disk-confirmation contract as single files (uncertain → dialog → retry)
		expect(pageSrc).toMatch(/prepareBundleWithDiskFlow[\s\S]*?askDiskConfirm/);
	});
	it("bundle conversion starts without a File (OPFS members are the source)", () => {
		expect(pageSrc).toMatch(/if \(!page\.prep \|\| \(!page\.file && !page\.bundleFiles\)\) return null;/);
		expect(runnerSrc).toMatch(/F3: a prepared bundle never needs the File again/);
	});
	it("MRXS output names drop the .mrxs suffix (entry stem)", () => {
		expect(E.outputFileName("CMU-1.mrxs", { result: { format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" } }))
			.toBe("CMU-1.ome.tif");
		expect(E.outputFileName("bf.kfb", { result: { format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" } }))
			.toBe("bf.ome.tif");
	});
	it("SVS output names drop the .svs suffix", () => {
		expect(E.outputFileName("CMU-1.svs", { result: { format: "ome-bigtiff-subifd-rgb-jpeg-pyramid" } }))
			.toBe("CMU-1.ome.tif");
		expect(E.outputFileName("CMU-1.SVS", { result: { format: "classic-bigtiff-jpeg-pyramid" } }))
			.toBe("CMU-1.tif");
	});
});
