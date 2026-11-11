/* =========================================================================
   本地直传引擎（P0-W6a）—— window.HP_LOCAL_UPLOAD

   本地裸部署（无腾讯云 COS 凭证）的上传通道：文件分片直接 POST 平台
   （/api/ingestions/<job>/parts/<n>，application/octet-stream），写 staging
   后复用服务端 ingestion 状态机与统一发布（validating→readiness→publish）。

   与 cos-uploader.js（HP_COS_UPLOAD）的关系：对外 createUpload(opts) 合同
   完全同形（source/apiFetch/config/storage/onEvent → {cancel, done,
   settled}），页面可按 capability 二选一（local_upload.available 优先本地；
   COS 配置后两通道并存）；差异全部在引擎内部——

     - 控制与分片全部经注入的 apiFetch（同源会话 + X-CSRF-Token，无预
       签名 URL、无跨域 PUT）；
     - 断点续传以**服务端账本为权威**：GET 任务状态在 uploading 态附
       received_parts（已收分片清单），pending = 计划 − 已收；重传同号
       幂等（服务端覆盖写）；
     - upload-complete 缺片 409 parts_incomplete（附 missing_parts）→
       引擎继续补缺片；齐全后服务端就地拼接转 queued。

   resolveConfig(payload)：capability local_upload → 规范化配置 | null
   （available!==true / 缺字段一律 null——宁可不用也不拿坏参数拼请求）。
   ========================================================================= */
(function () {
  "use strict";

  function resolveConfig(payload) {
    try {
      var c = payload;
      if (!c || c.available !== true) return null;
      var nums = {
        max_size_bytes: Number(c.max_size_bytes),
        part_bytes: Number(c.part_bytes),
        max_concurrent_parts: Number(c.max_concurrent_parts),
      };
      for (var k in nums) {
        if (typeof nums[k] !== "number" || !isFinite(nums[k]) || nums[k] <= 0) {
          return null;
        }
      }
      if (!Array.isArray(c.formats) || !c.formats.length) return null;
      var fmts = [];
      for (var i = 0; i < c.formats.length; i++) {
        if (typeof c.formats[i] !== "string" || !c.formats[i]) return null;
        fmts.push(c.formats[i].toLowerCase());
      }
      return {
        max_size_bytes: nums.max_size_bytes,
        part_bytes: nums.part_bytes,
        max_concurrent_parts: Math.max(1, Math.min(8, Math.floor(
          nums.max_concurrent_parts))),
        formats: fmts,
      };
    } catch (e) {
      return null;
    }
  }

  function jsonBody(r) {
    return r.json().then(function (b) { return { ok: r.ok, status: r.status, body: b }; },
                           function () { return { ok: r.ok, status: r.status, body: null }; });
  }

  // 计划（编号+长度，无 offset——与服务端冻结计划同构）→ 前端切片表
  function buildPlan(parts) {
    var out = [];
    var offset = 0;
    var sorted = parts.slice().sort(function (a, b) {
      return a.part_number - b.part_number; });
    for (var i = 0; i < sorted.length; i++) {
      out.push({ part_number: sorted[i].part_number,
                 offset: offset, length: sorted[i].length });
      offset += sorted[i].length;
    }
    return out;
  }

  function createUpload(opts) {
    var cfg = opts.config;
    var apiFetch = opts.apiFetch;
    var source = opts.source;
    var storage = opts.storage;
    var emit = opts.onEvent || function () {};
    var jobId = opts.resumeJobId || null;
    var account = typeof opts.account === "string" ? opts.account : "";
    var plan = null;
    var planByNum = {};
    var receivedMap = {};      // 服务端权威已收分片
    var confirmedBytes = 0;
    var totalBytes = 0;
    var stopped = false;
    var doneSettled = false;
    var waits = new Set();
    var settleCancelled = null;
    var cancelledOutcome = new Promise(function (r) { settleCancelled = r; });

    function delay(ms) {
      if (stopped) return Promise.reject({ cancelled: true });
      return new Promise(function (resolve, reject) {
        var w = { reject: reject };
        w.h = setTimeout(function () { waits.delete(w); resolve(); }, ms);
        waits.add(w);
      });
    }
    function stopTimers() {
      waits.forEach(function (w) { clearTimeout(w.h); w.reject({ cancelled: true }); });
      waits.clear();
    }
    function api(url, init) {
      if (stopped) return Promise.reject({ cancelled: true });
      return apiFetch(url, init);
    }
    function quiet(p) {
      if (p && typeof p.then === "function") p.then(null, function () {});
    }

    function fetchStatus() {
      return api("/api/ingestions/" + encodeURIComponent(jobId)).then(jsonBody);
    }

    function saveRecord(extra) {
      if (!storage || typeof storage.save !== "function") return null;
      var confirmed = [];
      for (var k in receivedMap) {
        if (receivedMap.hasOwnProperty(k)) confirmed.push(parseInt(k, 10));
      }
      return quiet(storage.save(Object.assign({
        job_id: jobId, filename: source.name, size: source.size,
        account: account, transport: "local_http",
        confirmed: confirmed,
      }, extra || {})));
    }

    // 单片：POST 原始字节（apiFetch 注入认证/CSRF；Blob 透传）。
    // 重试 ≤3（网络/5xx/429）；4xx（计划外/长度不符/状态冲突）不重试。
    function putPart(item) {
      var attempt = 0;
      function go() {
        if (stopped) return Promise.reject({ cancelled: true });
        attempt++;
        if (attempt > 1) {
          emit({ type: "retry", part: item.part_number, attempt: attempt });
        }
        var blob = source.slice(item.offset, item.offset + item.length);
        return api("/api/ingestions/" + encodeURIComponent(jobId) +
                   "/parts/" + item.part_number, {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body: blob,
        }).then(jsonBody).then(function (res) {
          if (res.ok) {
            receivedMap[item.part_number] = true;
            confirmedBytes += item.length;
            emit({ type: "progress", phase: "uploading", force: true,
                   frac: totalBytes ? confirmedBytes / totalBytes : 0,
                   loadedBytes: confirmedBytes, confirmedBytes: confirmedBytes,
                   totalBytes: totalBytes, determinate: true,
                   sentAll: confirmedBytes >= totalBytes });
            saveRecord();
            return null;
          }
          var b = res.body || {};
          if (b.received_parts) {
            b.received_parts.forEach(function (n) {
              receivedMap[n] = true;
            });
          }
          if ((res.status === 0 || res.status === 429 || res.status >= 500)
              && attempt < 3) {
            return delay(800).then(go);
          }
          throw { part: item.part_number, status: res.status, data: b };
        }, function (err) {
          if (err && err.cancelled) throw err;
          if (attempt < 3) return delay(800).then(go);
          throw { part: item.part_number, status: 0, network: true };
        });
      }
      return go();
    }

    function requestComplete(missing) {
      function go() {
        if (stopped) return Promise.reject({ cancelled: true });
        return api("/api/ingestions/" + encodeURIComponent(jobId) +
                   "/upload-complete", { method: "POST" })
          .then(jsonBody)
          .then(function (res) {
            if (res.ok) return drive();
            var b = res.body || {};
            if (res.status === 409 && b.code === "parts_incomplete" &&
                Array.isArray(b.missing_parts)) {
              // 服务端权威缺片清单 → 精确补传后重试
              return uploadPending(b.missing_parts).then(function () {
                return go();
              });
            }
            if (res.status === 409 && b.code === "part_files_missing") {
              return uploadPending().then(function () { return go(); });
            }
            throw { status: res.status, data: b };
          }, function (err) {
            if (err && err.cancelled) throw err;
            return delay(1500).then(function () { return fetchStatus()
              .then(function (r2) {
                var st = r2.ok && r2.body && r2.body.stage;
                if (st && st !== "uploading") return drive();
                return go();
              }, function () { return go(); });
            });
          });
      }
      return go();
    }

    function uploadPending(onlyNumbers) {
      var pending = plan.filter(function (p) {
        return !receivedMap[p.part_number] &&
          (!onlyNumbers || onlyNumbers.indexOf(p.part_number) >= 0);
      });
      if (!pending.length) return requestComplete();
      var conc = cfg.max_concurrent_parts || 2;
      var next = 0;
      var failure = null;
      function lane() {
        if (stopped) return Promise.reject({ cancelled: true });
        if (failure || next >= pending.length) return Promise.resolve();
        var item = pending[next++];
        return putPart(item).then(lane, function (e) {
          if (!failure && !stopped && !(e && (e.cancelled ||
              e.name === "AbortError"))) failure = e;
          return lane();
        });
      }
      var lanes = [];
      for (var k = 0; k < Math.min(conc, pending.length); k++) lanes.push(lane());
      return Promise.all(lanes).then(function () {
        if (failure) throw failure;
        return requestComplete();
      });
    }

    function freezePlan(parts) {
      var built = buildPlan(parts);
      var sum = 0;
      planByNum = {};
      for (var i = 0; i < built.length; i++) {
        planByNum[built[i].part_number] = built[i];
        sum += built[i].length;
      }
      if (source.size !== sum) {
        throw { status: 0, planMismatch: true,
                data: { code: "plan_size_mismatch",
                        error: "upload part plan does not match source size",
                        plan_bytes: sum, declared_bytes: source.size } };
      }
      plan = built;
      totalBytes = sum;
      confirmedBytes = 0;
    }

    var statusFails = 0;
    function transientOrThrow(status, data) {
      var transient = status === 0 || status === 429 || status >= 500;
      if (!transient) {
        statusFails = 0;
        throw { status: status, data: data };
      }
      statusFails++;
      if (statusFails > 5) {
        statusFails = 0;
        throw { status: status, data: data, recheck: true, network: status === 0 };
      }
      return delay(Math.min(1000 * Math.pow(2, statusFails - 1), 8000))
        .then(drive);
    }

    function drive() {
      return fetchStatus().then(function (res) {
        if (stopped) throw { cancelled: true };
        if (!res.ok) return transientOrThrow(res.status, res.body);
        statusFails = 0;
        var b = res.body || {};
        if (b.slide_id && jobId) quiet(saveRecord({ slide_id: b.slide_id }));
        if (b.warnings && b.warnings.length) {
          emit({ type: "warnings", warnings: b.warnings });  // mpp_missing 等
        }
        var st = b.stage;
        if (st === "waiting_space") {
          emit({ type: "status", body: b });
          return delay(5000).then(drive);
        }
        if (st === "uploading") {
          if (b.parts && b.parts.length) {
            if (!plan) {
              freezePlan(b.parts);
            } else {
              // 计划已冻结：仅吸收服务端已收清单（received_parts 权威）
            }
            if (Array.isArray(b.received_parts)) {
              b.received_parts.forEach(function (n) {
                if (!receivedMap[n] && planByNum[n]) {
                  receivedMap[n] = true;
                  confirmedBytes += planByNum[n].length;
                }
              });
            }
            return uploadPending();
          }
          emit({ type: "progress", frac: 0 });
          return delay(2000).then(drive);
        }
        if (st === "awaiting_server" || st === "downloading" ||
            st === "validating" || st === "processing" ||
            st === "readiness" || st === "viewable") {
          if (st === "viewable") return succeed(b);
          emit({ type: "status", body: b });
          return delay(2000).then(drive);
        }
        if (st === "terminal") {
          if (storage && typeof storage.complete === "function") {
            quiet(storage.complete(jobId, {
              terminal: true, fail_code: b.fail_code,
              fail_detail: b.fail_detail,
            }));
          }
          throw { terminal: true, data: b };
        }
        return delay(2000).then(drive);
      }, function (err) {
        if (err && err.cancelled) throw err;
        return transientOrThrow(0, null);
      });
    }

    function succeed(b) {
      if (storage && typeof storage.complete === "function") {
        quiet(storage.complete(jobId, {
          succeeded: true, slide_id: b.slide_id || null,
          warnings: b.warnings || [],
        }));
      }
      return { ok: true, body: b };
    }

    function createNewJob() {
      var createBody = Object.assign({
        filename: source.name, declared_size: source.size,
      }, (opts.createBody && typeof opts.createBody === "object")
        ? opts.createBody : {});
      if (account) createBody.expected_account = account;
      return api("/api/ingestions/local", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(createBody),
      }).then(jsonBody).then(function (res) {
        if (res.ok && res.body && res.body.job_id) {
          jobId = res.body.job_id;
          return Promise.resolve().then(function () { return saveRecord(); })
            .then(function () {
              emit({ type: "created", jobId: jobId, body: res.body });
              if (res.body.stage) emit({ type: "status", body: res.body });
              return null;
            }, function (cause) {
              // 记录落盘失败 = 弃单（与 cos 引擎同语义）
              return apiFetch("/api/ingestions/" +
                encodeURIComponent(jobId) + "/cancel", { method: "POST" })
                .then(jsonBody).then(function () {
                  throw { persist: true, ingestionId: jobId,
                          reconciled: true,
                          cause: cause && (cause.message || String(cause)) };
                });
            });
        }
        throw { status: res.status, data: res.body };
      });
    }

    function prepareResume() {
      if (jobId) {
        return Promise.resolve(
          storage && typeof storage.readRecord === "function"
            ? storage.readRecord(jobId)
            : { confirmed: [], account: "" }
        ).then(function (rec) {
          rec = rec || {};
          if (rec.account !== undefined && rec.account !== null &&
              String(rec.account) !== account) {
            jobId = null;      // 他号记录：本地视为无候选
            return null;
          }
          (rec.confirmed || []).forEach(function (n) {
            receivedMap[n] = true;
          });
          return null;         // 服务端 received_parts 才是权威，drive 会吸收
        });
      }
      var prev = storage && typeof storage.findResumable === "function"
        ? storage.findResumable(source) : null;
      if (!prev) return Promise.resolve(null);
      if (prev.account !== undefined && prev.account !== null &&
          String(prev.account) !== account) {
        return Promise.resolve(null);
      }
      var doResume = opts.skipConfirm ||
        (opts.confirmResume ? opts.confirmResume() : true);
      if (!doResume) {
        return api("/api/ingestions/" + encodeURIComponent(prev.job_id) +
                   "/cancel", { method: "POST" })
          .then(function () {
            if (storage && typeof storage.remove === "function") {
              quiet(storage.remove(prev.job_id));
            }
          });
      }
      jobId = prev.job_id;
      (prev.confirmed || []).forEach(function (n) { receivedMap[n] = true; });
      return Promise.resolve(null);
    }

    function cancel() {
      if (stopped || doneSettled) return;
      stopped = true;
      settleCancelled({ cancelled: true });
      stopTimers();
      if (jobId) {
        if (storage && typeof storage.remove === "function") {
          quiet(storage.remove(jobId));
        }
        apiFetch("/api/ingestions/" + encodeURIComponent(jobId) + "/cancel",
                 { method: "POST" }).catch(function () {});
      }
    }

    var main = Promise.resolve().then(function () {
      return prepareResume();
    }).then(function () {
      if (jobId) return null;
      return createNewJob();
    }).then(function () {
      return drive();
    }).then(null, function (err) {
      if (stopped || (err && err.cancelled)) return { cancelled: true };
      if (err instanceof TypeError) throw { network: true, status: 0, data: null };
      throw err;
    });
    var done = Promise.race([main, cancelledOutcome]);
    done.then(function () { doneSettled = true; },
              function () { doneSettled = true; });

    return {
      cancel: cancel,
      done: done,
      settled: function () { return stopped || doneSettled; },
    };
  }

  window.HP_LOCAL_UPLOAD = {
    resolveConfig: resolveConfig,
    createUpload: createUpload,
    buildPlan: buildPlan,
  };
})();
