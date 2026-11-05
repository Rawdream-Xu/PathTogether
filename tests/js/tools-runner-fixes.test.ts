/**
 * Runner/worker engine-layer regressions (audit converter-redesign 2026-10-21).
 *
 * These drive the PRODUCTION runner.js/worker.js in Node against a small
 * in-memory OPFS + Web Locks + Worker fake. They are behavioural: the same
 * file can be pointed at an old copy of the module directory with
 * `ST_TRANSFORM_DIR=/path/to/old/slide-transform` to prove each regression
 * fails before the fix (see the runnerfix report).
 *
 * Covered:
 *   1 worker.openBundle: the member cache is keyed by jobId (preparing B
 *     after A and starting A must not convert B's members)
 *   2 runner: a second start/resume while one is in flight is refused with
 *     `job_busy_this_tab`; the running job's jobId/done promise are untouched
 *   3 cancelJob's done value carries `type: 'cancelled'`; a cancel racing the
 *     record read resolves cancelled instead of job_dir_missing
 *   4 discardJob of the converting job terminates the worker first (prompt,
 *     no release-source request behind the synchronous wasm call)
 *   5 exportJob/artifactView: missing output → typed artifact_missing
 *   6 _summary: a recoverable failed record with staged source + journal
 *     generation offers nextAction 'resume'; other failures stay 'discard'
 */
import { afterAll, beforeEach, beforeAll, describe, expect, it } from "vitest";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const TRANSFORM_DIR = process.env.ST_TRANSFORM_DIR
	? resolve(process.env.ST_TRANSFORM_DIR)
	: resolve(here, "../../static/tools/slide-transform");

const enc = new TextEncoder();
const dec = new TextDecoder();
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- fakes --

function notFound(what = "entry") {
	const e: any = new Error(
		"A requested file or directory could not be found at the time an operation was processed. (" + what + ")");
	e.name = "NotFoundError";
	return e;
}

class FakeFile {
	data: Uint8Array;
	constructor(data: Uint8Array) { this.data = data; }
	get size() { return this.data.length; }
	slice(a = 0, b = this.data.length) {
		const part = this.data.slice(a, Math.min(b, this.data.length));
		return {
			size: part.length,
			arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength),
		};
	}
	stream() { return new Blob([this.data]).stream(); }
}

class FakeFileHandle {
	kind = "file";
	dir: FakeDir;
	name: string;
	constructor(dir: FakeDir, name: string) { this.dir = dir; this.name = name; }
	async getFile() { return new FakeFile(this.dir._files.get(this.name) || new Uint8Array(0)); }
	async createWritable() {
		const dir = this.dir;
		const name = this.name;
		return {
			write: async (chunk: any) => {
				if (chunk instanceof Uint8Array) { dir._files.set(name, chunk.slice()); return; }
				if (chunk && chunk.data instanceof Uint8Array) { dir._files.set(name, chunk.data.slice()); return; }
				throw new TypeError("unsupported write chunk");
			},
			close: async () => {},
			abort: async () => {},
		};
	}
	async createSyncAccessHandle() {
		const dir = this.dir;
		const name = this.name;
		const data = () => dir._files.get(name) || new Uint8Array(0);
		return {
			closed: false,
			getSize: () => data().length,
			read: (u8: Uint8Array, opts: { at: number }) => {
				const d = data();
				const n = Math.max(0, Math.min(u8.length, d.length - opts.at));
				u8.set(d.subarray(opts.at, opts.at + n));
				return n;
			},
			write: (u8: Uint8Array, opts: { at: number }) => {
				const cur = data();
				const end = Math.max(cur.length, opts.at + u8.length);
				const out = new Uint8Array(end);
				out.set(cur);
				out.set(u8, opts.at);
				dir._files.set(name, out);
				return u8.length;
			},
			truncate: (len: number) => {
				const cur = data();
				const out = new Uint8Array(len);
				out.set(cur.subarray(0, Math.min(len, cur.length)));
				dir._files.set(name, out);
			},
			flush: () => {},
			close(this: any) { this.closed = true; },
		};
	}
}

class FakeDir {
	kind = "directory";
	name: string;
	_files = new Map<string, Uint8Array>();
	_dirs = new Map<string, FakeDir>();
	constructor(name = "") { this.name = name; }
	async getDirectoryHandle(name: string, opts: any = {}) {
		let d = this._dirs.get(name);
		if (!d) {
			if (!opts.create) throw notFound(name);
			d = new FakeDir(name);
			this._dirs.set(name, d);
		}
		return d;
	}
	async getFileHandle(name: string, opts: any = {}) {
		if (!this._files.has(name)) {
			if (!opts.create) throw notFound(name);
			this._files.set(name, new Uint8Array(0));
		}
		return new FakeFileHandle(this, name);
	}
	async removeEntry(name: string, opts: any = {}) {
		if (this._files.delete(name)) return;
		if (opts.recursive && this._dirs.delete(name)) return;
		if (!opts.recursive && this._dirs.has(name)) throw new Error("directory not empty");
		throw notFound(name);
	}
	async *entries() {
		for (const [n, d] of this._dirs) yield [n, d];
		for (const [n] of this._files) yield [n, new FakeFileHandle(this, n)];
	}
}

let root = new FakeDir("");
let locksHeld = new Set<string>();

const locks = {
	async request(name: string, a: any, b?: any) {
		const opts = typeof a === "function" ? {} : a;
		const cb = typeof a === "function" ? a : b;
		if (opts && opts.ifAvailable && locksHeld.has(name)) return cb(null);
		locksHeld.add(name);
		try {
			return await cb({ name });
		} finally {
			locksHeld.delete(name);
		}
	},
};

class FakeWorker {
	static instances: FakeWorker[] = [];
	static autoReply: ((w: FakeWorker, m: any) => any) | null = null;
	/// init 行为：'ready'（默认）| 'init-failed' | 'error' | 'silent'
	static initBehavior: "ready" | "init-failed" | "error" | "silent" = "ready";
	static startWithBoot = false;
	handlers: Record<string, Array<(ev: any) => void>> = {};
	posted: any[] = [];
	starts: any[] = [];
	terminated = false;
	constructor(_url: URL, _opts?: any) { FakeWorker.instances.push(this); }
	addEventListener(kind: string, fn: (ev: any) => void) {
		(this.handlers[kind] || (this.handlers[kind] = [])).push(fn);
	}
	postMessage(m: any) {
		this.posted.push(m);
		if (m.type === "init") {
			const behavior = FakeWorker.initBehavior;
			queueMicrotask(() => {
				if (FakeWorker.startWithBoot) this.emit({ type: "booting" });
				if (behavior === "ready") {
					this.emit({ type: "ready", coreVersion: "test-core" });
				} else if (behavior === "init-failed") {
					this.emit({ type: "init-failed", reason: "wasm_fetch",
						message: "转换组件下载失败：HTTP 0" });
				} else if (behavior === "error") {
					this.emitError("Failed to fetch dynamically imported module");
				}
			});
			return;
		}
		if (m.type === "start") this.starts.push(m);
		if (FakeWorker.autoReply) {
			const r = FakeWorker.autoReply(this, m);
			if (r !== undefined) {
				queueMicrotask(() => this.emit({
					type: "reply", id: m.id, ok: !(r && r.error), result: r,
				}));
			}
		}
	}
	emit(data: any) {
		for (const fn of this.handlers.message || []) fn({ data });
	}
	emitError(message: string) {
		for (const fn of this.handlers.error || []) fn({ message, preventDefault() {} });
	}
	emitMessageError() {
		for (const fn of this.handlers.messageerror || []) fn({});
	}
	terminate() { this.terminated = true; }
	static last(): FakeWorker { return FakeWorker.instances[FakeWorker.instances.length - 1]; }
}

// --------------------------------------------------------------- modules --

let E: any;
let Runner: any;
let WorkerMod: any;

beforeAll(async () => {
	(globalThis as any).self = { postMessage() {} };
	(globalThis as any).Worker = FakeWorker;
	Object.defineProperty(globalThis, "navigator", {
		configurable: true,
		value: {
			storage: {
				getDirectory: async () => root,
				estimate: async () => ({ usage: 0, quota: 2 ** 40 }),
			},
			locks,
			hardwareConcurrency: 4,
		},
	});
	E = await import(/* @vite-ignore */ pathToFileURL(resolve(TRANSFORM_DIR, "engine.js")).href);
	Runner = await import(/* @vite-ignore */ pathToFileURL(resolve(TRANSFORM_DIR, "runner.js")).href);
	WorkerMod = await import(/* @vite-ignore */ pathToFileURL(resolve(TRANSFORM_DIR, "worker.js")).href);
});

beforeEach(() => {
	root = new FakeDir("");
	locksHeld = new Set<string>();
	FakeWorker.instances = [];
	FakeWorker.autoReply = null;
	FakeWorker.initBehavior = "ready";
	FakeWorker.startWithBoot = false;
});

afterAll(() => {
	FakeWorker.autoReply = null;
});

// ---------------------------------------------------------------- helpers --

async function writeFile(dir: FakeDir, name: string, data: Uint8Array) {
	const fh = await dir.getFileHandle(name, { create: true });
	const w = await fh.createWritable();
	await w.write(data);
	await w.close();
}

function probeResult() {
	const estimate = { output_upper_bound_bytes: 4096, cells_total: 4, tiles_present: 4 };
	return {
		document: {
			modality: "brightfield",
			levels: [{}, {}],
			channels: [],
			estimate,
		},
		estimate,
		adapter: null,
	};
}

function preparedRecord(overrides: Record<string, any> = {}) {
	return {
		state: "prepared",
		bundle: false,
		identity: { name: "a.kfb", size: 16, lastModified: 1, sha256: "a".repeat(64) },
		core: "test-core",
		estimate: { output_upper_bound_bytes: 4096, cells_total: 4, tiles_present: 4 },
		modality: "brightfield",
		sourceAdapter: null,
		outputProfile: "bf-ome",
		encodingProfile: "preserve-source-v1",
		channelJson: null,
		channelJsonHash: null,
		profile: "saver",
		policy: "allow-edge",
		cap: null,
		...overrides,
	};
}

function journalGen(overrides: Record<string, any> = {}) {
	return {
		t: "gen",
		gen: 1,
		identity: { name: "a.kfb", size: 16, sha256: "a".repeat(64) },
		core: "test-core",
		plan: 1,
		policy: "allow-edge",
		profile: "saver",
		outputProfile: "bf-ome",
		sourceAdapter: null,
		adapterVersion: null,
		encodingProfile: "preserve-source-v1",
		cap: null,
		resume: null,
		...overrides,
	};
}

async function seedJob(id: string, rec: Record<string, any>, opts: {
	journal?: any[];
	output?: Uint8Array;
} = {}) {
	const jobs = await root.getDirectoryHandle("slide-jobs", { create: true });
	const dir = await jobs.getDirectoryHandle(id, { create: true });
	await E.writeSlotRecord(dir, "job", {
		v: 1,
		id,
		createdAt: "2026-01-01T00:00:00.000Z",
		updatedAt: "2026-01-01T00:00:00.000Z",
		...rec,
	});
	if (opts.journal) {
		await writeFile(dir, E.JOURNAL_FILE,
			enc.encode(opts.journal.map((r) => E.encodeJournalRecord(r)).join("")));
	}
	if (opts.output) await writeFile(dir, E.OUTPUT_NAME, opts.output);
	return dir;
}

async function seedBundle(id: string, member: string, bytes: Uint8Array) {
	const jobs = await root.getDirectoryHandle("slide-jobs", { create: true });
	const dir = await jobs.getDirectoryHandle(id, { create: true });
	const bundle = await dir.getDirectoryHandle("bundle", { create: true });
	const manifest = {
		v: 1,
		adapter: "mrxs-bundle",
		members: [{ path: member, size: bytes.length, sha256: "0".repeat(64) }],
		memberCount: 1,
		totalBytes: bytes.length,
	};
	await writeFile(bundle, "manifest.json", enc.encode(JSON.stringify(manifest)));
	await writeFile(bundle, member, bytes);
	return dir;
}

async function makeRunner(auto: (m: any) => any = (m: any) =>
	(m.type === "probe" || m.type === "probe-bundle" ? probeResult() : undefined)) {
	FakeWorker.autoReply = (_w, m) => auto(m);
	const runner = await Runner.SlideToolsRunner.create();
	// after create(), the worker instance exists
	return { runner, worker: FakeWorker.last() };
}

async function dirExists(id: string) {
	try {
		const jobs = await root.getDirectoryHandle("slide-jobs");
		await jobs.getDirectoryHandle(id);
		return true;
	} catch { return false; }
}

function emitDone(worker: FakeWorker, extra: Record<string, any> = {}) {
	worker.emit({
		type: "done", ok: true, phase: "validated",
		result: { output_bytes: 10 }, validation: { sha256: "f".repeat(64) },
		convertMs: 1, wasmHeapPeak: 1, journalBytes: 1,
		...extra,
	});
}

// ----------------------------------------------------------------- tests --

describe("worker bundle cache (bug 1)", () => {
	it("openBundle is keyed by jobId: a second prepared bundle is not served A's members", async () => {
		await seedBundle("job-A", "a.dat", new Uint8Array([1, 2, 3]));
		await seedBundle("job-B", "b.dat", new Uint8Array([4, 5, 6, 7]));
		const a = await WorkerMod.openBundle("job-A");
		expect(a.map((m: any) => m.name)).toEqual(["a.dat"]);
		const firstHandle = a[0].handle;
		const b = await WorkerMod.openBundle("job-B");
		expect(b.map((m: any) => m.name)).toEqual(["b.dat"]);
		expect(b[0].size).toBe(4);
		// reopening another job closes the previous job's sync handles
		expect(firstHandle.closed).toBe(true);
		// and reopening the same job still reuses the cache
		const b2 = await WorkerMod.openBundle("job-B");
		expect(b2).toBe(b);
	});
});

describe("one conversion per tab (bug 2)", () => {
	it("a second start while one is in flight is refused with job_busy_this_tab", async () => {
		await seedJob("job-A", preparedRecord());
		await seedJob("job-B", preparedRecord({ identity: { name: "b.kfb", size: 8, lastModified: 2, sha256: "b".repeat(64) } }));
		const { runner, worker } = await makeRunner();

		const a = await runner.startJob(null, { jobId: "job-A" });
		expect(a.jobId).toBe("job-A");
		const doneA = a.done;

		await expect(runner.startJob(null, { jobId: "job-B" }))
			.rejects.toMatchObject({ error: { code: "job_busy_this_tab" } });
		await expect(runner.resumeJob("job-B"))
			.rejects.toMatchObject({ error: { code: "job_busy_this_tab" } });

		// the running job's identity and completion promise were not touched
		expect(runner.jobId).toBe("job-A");
		expect(typeof runner._doneResolve).toBe("function");
		expect(worker.starts.map((m) => m.jobId)).toEqual(["job-A"]);

		emitDone(worker);
		const res = await doneA;
		expect(res.ok).toBe(true);
		const jobA = await runner.getJob("job-A");
		const jobB = await runner.getJob("job-B");
		expect(jobA.state).toBe("ready");
		expect(jobA.result.outputBytes).toBe(10);
		expect(jobB.state).toBe("prepared");
		expect(runner._activeStart).toBe(null);
	});

	it("probe/prepareBundle of a new input are refused while a conversion runs", async () => {
		await seedJob("job-A", preparedRecord());
		const { runner, worker } = await makeRunner();
		const a = await runner.startJob(null, { jobId: "job-A" });
		const file = new Blob([new Uint8Array([0xf1, 0x01, 0xee, 0xee, 0x4b, 0x46, 0x42, 0x00])]);
		await expect(runner.probe(file))
			.rejects.toMatchObject({ error: { code: "job_busy_this_tab" } });
		await expect(runner.prepareBundle([{ name: "x.vms", relPath: "x.vms", file }]))
			.rejects.toMatchObject({ error: { code: "job_busy_this_tab" } });
		// the running job still owns this.jobId and its done promise
		expect(runner.jobId).toBe("job-A");
		emitDone(worker);
		expect((await a.done).ok).toBe(true);
	});

	it("a preparation failure inside start keeps its typed error (discard cleanup never cancels)", async () => {
		const estimate = { output_upper_bound_bytes: 4096, cells_total: 1, tiles_present: 1 };
		const { runner, worker } = await makeRunner((m) => {
			if (m.type === "stage-source") return { size: 16, sha256: "a".repeat(64) };
			if (m.type === "probe") {
				return {
					document: { modality: "fluorescence", levels: [{}], channels: [{}], estimate },
					estimate,
				};
			}
			if (m.type === "release-source" || m.type === "release-bundle") return {};
			return undefined;
		});
		const file = new Blob([new Uint8Array([0xf1, 0x01, 0xee, 0xee, 0x4b, 0x46, 0x42, 0x46])]);
		const failure = await runner.startJob(file, { encodingProfile: "compact-jpeg-v1" })
			.then(() => null, (e: any) => e);
		expect(failure).toMatchObject({ error: { code: "unsupported_input" } });
		expect(runner._activeStart ?? null).toBe(null);
		expect(worker.terminated).toBe(false);
	});
});

describe("cancel semantics (bug 3)", () => {
	it("the done value carries type:'cancelled' with the legacy fields", async () => {
		await seedJob("job-A", preparedRecord());
		const { runner, worker } = await makeRunner();
		const a = await runner.startJob(null, { jobId: "job-A" });
		const c = await runner.cancelJob();
		expect(c.cancelled).toBe(true);
		const res = await a.done;
		expect(res).toMatchObject({
			type: "cancelled", cancelled: true, ok: false, phase: "cancelled", jobId: "job-A",
		});
		expect(worker.terminated).toBe(true);
		expect(await dirExists("job-A")).toBe(false);
	});

	it("cancel racing the record read resolves cancelled, not job_dir_missing", async () => {
		await seedJob("job-B", preparedRecord({
			state: "paused",
			profile: "saver",
		}), {
			journal: [journalGen()],
			output: new Uint8Array([0]),
		});
		const { runner, worker } = await makeRunner((m) => {
			if (m.type === "verify-source") return { size: 16, sha256: "a".repeat(64) };
			if (m.type === "probe") return probeResult();
			return undefined;
		});
		// the page had prepared B, so a cancel can find its dir even before
		// resumeJob installs the active token
		runner.jobId = "job-B";
		const original = runner._readJobRecord.bind(runner);
		let calls = 0;
		let releaseRead!: () => void;
		let readStarted!: () => void;
		const gate = new Promise<void>((r) => { releaseRead = r; });
		const started = new Promise<void>((r) => { readStarted = r; });
		runner._readJobRecord = async (id: string) => {
			calls += 1;
			if (calls === 2) {
				readStarted();
				await gate;
				throw notFound("job dir removed by cancel");
			}
			return original(id);
		};

		const p = runner.resumeJob("job-B");
		await started;
		await runner.cancelJob();
		releaseRead();
		const res = await p; // must NOT reject with job_dir_missing
		expect(res.jobId).toBe("job-B");
		expect(await res.done).toMatchObject({ type: "cancelled", cancelled: true });
		expect(worker.terminated).toBe(true);
	});
});

describe("discard of the converting job (bug 4)", () => {
	it("terminates the worker first and deletes promptly (no release-source behind the wasm call)", async () => {
		await seedJob("job-A", preparedRecord());
		const { runner, worker } = await makeRunner(); // release-source is never answered
		const a = await runner.startJob(null, { jobId: "job-A" });

		const t0 = Date.now();
		const dp = runner.discardJob("job-A");
		const outcome = await Promise.race([
			dp.then(() => "done", () => "error"),
			sleep(1500).then(() => "timeout"),
		]);
		const elapsed = Date.now() - t0;
		if (outcome === "timeout") {
			// let the old implementation's 15-minute request timer go
			runner._rejectPending("test cleanup");
			await dp.catch(() => {});
		}
		expect(outcome).toBe("done");
		expect(elapsed).toBeLessThan(1200);
		expect(worker.terminated).toBe(true);
		expect(await dirExists("job-A")).toBe(false);
		const res = await a.done;
		expect(res).toMatchObject({ type: "cancelled", cancelled: true });
	});
});

describe("missing artifact (bug 5)", () => {
	it("exportJob and artifactView report typed artifact_missing when output.tif is evicted", async () => {
		await seedJob("job-R", preparedRecord({ state: "ready" }));
		const { runner } = await makeRunner();
		const writable = { write: async () => {}, close: async () => {}, abort: async () => {} };
		await expect(runner.exportJob("job-R", async () => writable))
			.rejects.toMatchObject({ error: { code: "artifact_missing" } });
		await expect(runner.artifactView("job-R"))
			.rejects.toMatchObject({ error: { code: "artifact_missing" } });
	});

	it("keeps not_ready_not_exportable for a non-ready record", async () => {
		await seedJob("job-P", preparedRecord({ state: "planned" }));
		const { runner } = await makeRunner();
		await expect(runner.artifactView("job-P"))
			.rejects.toMatchObject({ error: { code: "not_ready_not_exportable" } });
	});

	it("a present artifact still opens through artifactView", async () => {
		await seedJob("job-W", preparedRecord({ state: "ready" }), { output: new Uint8Array([9, 8, 7]) });
		const { runner } = await makeRunner();
		const f = await runner.artifactView("job-W");
		expect(f.size).toBe(3);
	});
});

describe("worker loss (review regression)", () => {
	async function settleWithin(done: Promise<any>, ms = 600) {
		return Promise.race([
			done.then((r: any) => ({ r }), (e: any) => ({ e })),
			sleep(ms).then(() => null),
		]);
	}

	it("worker 'error' during a run resolves a crashed done and frees the runner", async () => {
		await seedJob("job-A", preparedRecord(), { journal: [journalGen()] });
		await seedJob("job-B", preparedRecord({
			identity: { name: "b.kfb", size: 8, lastModified: 2, sha256: "b".repeat(64) },
		}));
		const { runner, worker } = await makeRunner((m) => {
			if (m.type === "probe") return probeResult();
			if (m.type === "stage-source") return { size: 16, sha256: "c".repeat(64) };
			return undefined;
		});
		const a = await runner.startJob(null, { jobId: "job-A" });
		const done = a.done;
		worker.emitError("Aborted(OOM)");

		const outcome = await settleWithin(done);
		if (!outcome) {
			// reviewed implementation: the crash left the done pending
			await runner.terminateWorkerForTest();
			await done.catch(() => {});
		}
		expect(outcome).not.toBe(null);
		expect(outcome!.r).toMatchObject({
			jobId: "job-A", ok: false, phase: "worker-exit", crashed: true,
			error: { code: "io_recoverable", kind: "worker-exit", reason: "error" },
		});
		// no lingering active job / heavy lock / dead worker
		expect(runner._activeStart ?? null).toBe(null);
		expect(runner._lockRelease ?? null).toBe(null);
		expect(runner.worker).toBe(null);

		// a crash after the journal generation is resumable and offered as such
		const crashedJob = await runner.getJob("job-A");
		expect(crashedJob.state).toBe("failed");
		expect(crashedJob.error.code).toBe("io_recoverable");
		expect(crashedJob.nextAction).toBe("resume");

		// the user can start again immediately, without reload
		const b = await runner.startJob(null, { jobId: "job-B" });
		expect(b.jobId).toBe("job-B");
		emitDone(FakeWorker.last());
		expect((await b.done).ok).toBe(true);
	});

	it("'messageerror' uses the same crash path", async () => {
		await seedJob("job-M", preparedRecord(), { journal: [journalGen()] });
		const { runner, worker } = await makeRunner();
		const a = await runner.startJob(null, { jobId: "job-M" });
		const done = a.done;
		worker.emitMessageError();
		const outcome = await settleWithin(done);
		if (!outcome) {
			await runner.terminateWorkerForTest();
			await done.catch(() => {});
		}
		expect(outcome).not.toBe(null);
		expect(outcome!.r).toMatchObject({
			phase: "worker-exit", crashed: true,
			error: { code: "io_recoverable", reason: "messageerror" },
		});
		expect(runner._activeStart ?? null).toBe(null);
		expect(runner._lockRelease ?? null).toBe(null);
	});

	it("terminateWorkerForTest uses the same crash path (typed error + resume)", async () => {
		await seedJob("job-T", preparedRecord(), { journal: [journalGen()] });
		const { runner } = await makeRunner();
		const a = await runner.startJob(null, { jobId: "job-T" });
		await runner.terminateWorkerForTest();
		const res = await a.done;
		expect(res).toMatchObject({
			jobId: "job-T", ok: false, phase: "worker-exit", crashed: true,
			error: { code: "io_recoverable", reason: "terminated" },
		});
		const job = await runner.getJob("job-T");
		expect(job.state).toBe("failed");
		expect(job.nextAction).toBe("resume");
		expect(runner._activeStart ?? null).toBe(null);
	});

	it("a crash before the journal generation leaves the record startable", async () => {
		await seedJob("job-E", preparedRecord());
		const { runner, worker } = await makeRunner();
		const a = await runner.startJob(null, { jobId: "job-E" });
		const done = a.done;
		worker.emitError("worker failed during setup");
		const outcome = await settleWithin(done);
		if (!outcome) {
			await runner.terminateWorkerForTest();
			await done.catch(() => {});
		}
		expect(outcome).not.toBe(null);
		const job = await runner.getJob("job-E");
		// no journal gen → not marked failed (marking it would lose 'start')
		expect(job.state).toBe("planned");
		expect(job.nextAction).toBe("start");
	});
});

describe("recoverable failed jobs (bug 6)", () => {
	it("failed + recoverable code + staged source + journal generation offers resume", async () => {
		const { runner } = await makeRunner();
		await seedJob("job-quota", preparedRecord({
			state: "failed", error: { code: "quota_exceeded_recoverable", message: "quota" },
		}), { journal: [journalGen()], output: new Uint8Array([0]) });
		await seedJob("job-io", preparedRecord({
			state: "failed", error: { code: "io_recoverable", message: "io" },
		}), { journal: [journalGen()], output: new Uint8Array([0]) });
		await seedJob("job-fatal", preparedRecord({
			state: "failed", error: { code: "unsupported_input", message: "bad" },
		}), { journal: [journalGen()], output: new Uint8Array([0]) });
		await seedJob("job-nojournal", preparedRecord({
			state: "failed", error: { code: "io_recoverable", message: "io" },
		}), { output: new Uint8Array([0]) });
		await seedJob("job-nogen", preparedRecord({
			state: "failed", error: { code: "io_recoverable", message: "io" },
		}), { journal: [{ t: "c", gen: 1, seq: 1, st: { out: 0 }, chunks: [] }], output: new Uint8Array([0]) });
		await seedJob("job-planned", preparedRecord({ state: "planned" }), {
			journal: [journalGen()], output: new Uint8Array([0]),
		});

		const byId = Object.fromEntries((await runner.listJobs()).map((j: any) => [j.id, j]));
		expect(byId["job-quota"].nextAction).toBe("resume");
		expect(byId["job-io"].nextAction).toBe("resume");
		expect(byId["job-fatal"].nextAction).toBe("discard");
		expect(byId["job-nojournal"].nextAction).toBe("discard");
		expect(byId["job-nogen"].nextAction).toBe("discard");
		expect(byId["job-planned"].nextAction).toBe("resume");
	});

	it("a recoverable failed job actually resumes through resumeJob", async () => {
		const { runner, worker } = await makeRunner((m) => {
			if (m.type === "verify-source") return { size: 16, sha256: "a".repeat(64) };
			if (m.type === "probe") return probeResult();
			return undefined;
		});
		await seedJob("job-resume", preparedRecord({
			state: "failed", error: { code: "io_recoverable", message: "io" },
		}), { journal: [journalGen()], output: new Uint8Array([0]) });
		const started = await runner.resumeJob("job-resume");
		expect(started.jobId).toBe("job-resume");
		emitDone(worker);
		const res = await started.done;
		expect(res.ok).toBe(true);
		expect((await runner.getJob("job-resume")).state).toBe("ready");
	});
});

// --------------------------------------------------------------------------- //
// 2026-11-05 计划 A：转换器初始化快速失败、带原因、可真正重试
// --------------------------------------------------------------------------- //
describe("converter init fast-fail", () => {
	it("init-failed → 立即 converter_unavailable + reason（不等 20s），worker 被终止", async () => {
		FakeWorker.initBehavior = "init-failed";
		const t0 = Date.now();
		const err: any = await Runner.SlideToolsRunner.create()
			.catch((e: any) => e);
		expect(Date.now() - t0).toBeLessThan(2000); // 绝不是超时路径
		expect(err.error.code).toBe("converter_unavailable");
		expect(err.error.reason).toBe("wasm_fetch");
		expect(FakeWorker.last().terminated).toBe(true);
	});

	it("初始化期间 worker error → module_load_failed（立即）", async () => {
		FakeWorker.initBehavior = "error";
		const t0 = Date.now();
		const err: any = await Runner.SlideToolsRunner.create()
			.catch((e: any) => e);
		expect(Date.now() - t0).toBeLessThan(2000);
		expect(err.error.code).toBe("converter_unavailable");
		expect(err.error.reason).toBe("module_load_failed");
	});

	it("booting 之后仍正常收到 ready（慢网络窗口重置）", async () => {
		FakeWorker.startWithBoot = true;
		const runner = await Runner.SlideToolsRunner.create();
		expect(runner.coreVersion).toBe("test-core");
	});

	it("失败后重试：新建 worker（旧实例已终止）并成功", async () => {
		FakeWorker.initBehavior = "init-failed";
		const runner = new Runner.SlideToolsRunner();
		const first: any = await runner._init().catch((e: any) => e);
		expect(first.error.reason).toBe("wasm_fetch");
		expect(FakeWorker.instances).toHaveLength(1);
		expect(runner.worker).toBeNull();

		FakeWorker.initBehavior = "ready";
		await runner._init();
		expect(FakeWorker.instances).toHaveLength(2);
		expect(FakeWorker.instances[0].terminated).toBe(true);
		expect(FakeWorker.instances[1].terminated).toBe(false);
		expect(runner.coreVersion).toBe("test-core");
	});

	it("缺 OPFS（navigator.storage.getDirectory）→ browser_unsupported:OPFS，且不创建 worker", async () => {
		const saved = Object.getOwnPropertyDescriptor(globalThis, "navigator");
		Object.defineProperty(globalThis, "navigator", {
			configurable: true,
			value: { storage: {}, locks },
		});
		const before = FakeWorker.instances.length;
		const err: any = await Runner.SlideToolsRunner.create()
			.catch((e: any) => e);
		expect(err.error.code).toBe("converter_unavailable");
		expect(err.error.reason).toBe("browser_unsupported:OPFS");
		expect(FakeWorker.instances.length).toBe(before); // 未下载/未启动 worker
		if (saved) Object.defineProperty(globalThis, "navigator", saved);
	});

	it("缺 WebAssembly → browser_unsupported:WebAssembly", async () => {
		const saved = (globalThis as any).WebAssembly;
		Object.defineProperty(globalThis, "WebAssembly", {
			configurable: true, value: undefined, writable: true,
		});
		try {
			const err: any = await Runner.SlideToolsRunner.create()
				.catch((e: any) => e);
			expect(err.error.reason).toBe("browser_unsupported:WebAssembly");
		} finally {
			Object.defineProperty(globalThis, "WebAssembly", {
				configurable: true, value: saved, writable: true,
			});
		}
	});
});
