#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传容量存量核账与修复（0072/0073；R12 §3.4/§3.5/§4 + R13 修订）。

模型：只补**容量责任**，不恢复执行——异常任务先停止（failed/cancelled +
清理 pending），按冻结证据补记责任；本轮不实现损坏任务自动续传。
正常、绑定一致的活跃任务不改状态；配额豁免身份（owner/本地模式）按身份
合同显式识别（预约为空 ≠ 异常）；普通用户不能靠 rid 为空获得豁免。

工作流：
  1. 在线 dry-run（默认）：只读预审报告，不是可应用计划；
  2. 维护窗口（停写 + 停旧 API/worker/重试器/子进程 + 备份）内
     ``--plan-out plan.json``：带 schema/工具版本、数据根身份、DB 前态
     （含裁决字段：owner/预约 owner/state/holder/purpose/金额/pending
     清单）与文件证据（逐成员相对路径/类型/size/sha256；硬链接按 inode
     去重计量）的冻结计划。数据库口令/邮件配置/COS 密钥/分享 token 不进
     入证据；
  3. ``--apply --plan plan.json``：**回执优先**——先识别已应用动作并核验
     其合法后继（仍持有 / 已合法结算清理），未应用动作才要求原前态与原
     文件清单（正常清理后的重跑不是漂移）；随后在同一应用事务内重验完整
     前态（目标行 + 预约行 FOR UPDATE 锁定 + 计数 + pending 子集 + 计划
     外/失效动作 + 新 blocker），**任何一项不过即在任何写入前阻断**
     （退出 3，数据与回执零变化）；全过才逐动作应用并原子写回执（0073
     action_key 幂等，repair 回执持久记录新 reservation_id）；
  4. 应用后重新 collect/verify，输出**应用后**的 over_quota/pending/
     阻断项。

退出码：0=检查完整且无未解释责任（已补记可解释的超额可 0，但列明并注明
「继续禁止新准入」）；2=参数/环境错误（含旧 --reattach、--repair-residuals
与冻结计划不一致）；3=证据缺失/漂移/mismatch/未处理责任。预约缺失但仍有
未补记残留不得返回 0。

边界：核账不删除任何本地/远端数据、不调清理 worker、不改额度
（quota_bytes 不变）、不自动扣第二次 used；consumed（活跃或终态）一律
阻断人工核对——暂存残留与已发布对象/已结算源的资产关系无法在核账内证明
独立，不得自动补第二份责任；跨 owner/holder/purpose 错/共享预算归因
不明 → 阻断。生产执行另行批准。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import stat as _stat
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
import psycopg.rows

import pg_store
import slide_storage
import upload_guard

SCHEMA_VERSION = 1
TOOL_VERSION = "r15.1"

_ACTIVE_UPLOAD_TASK_STATES = ("active", "committing")
_ACTIVE_INGESTION_STATES = ("preparing", "uploading", "completing", "queued",
                            "downloading", "validating")
# C5 producer 导入：活跃 = 非 done 且有未收口责任（final/scratch 预约、双侧
# 清理 duty、未裁决 commit intent——非 done 行至少持有其一；done 是双侧清理
# 收口后的唯一终态）。
_ACTIVE_PRODUCER_SQL = "state <> 'done'"
_PURPOSE = {"upload_task": "upload", "ingestion_job": "ingest_local",
            "baidu_batch": "baidu_import"}
# producer_import 同一 import_id 持 final/scratch 两份不同用途预约（C5 §4.1）
# ——用途合法性按预约行逐份判定（classify_producer），不进单值 _PURPOSE。
_KIND_PURPOSE_SETS = {
    "producer_import": frozenset({"final", "scratch"}),
}
_HOLDER_ID_KEY = {"upload_task": "upload_id", "ingestion_job": "job_id",
                  "baidu_batch": "batch_id",
                  "producer_import": "import_id"}
_TARGET_SQL = {
    "upload_task": "SELECT state, reservation_id, owner_user_id,"
                   " quota_mode,"
                   " (commit_intent_json IS NOT NULL) AS has_intent,"
                   " (commit_token IS NOT NULL) AS has_token FROM"
                   " upload_tasks WHERE upload_id=%s",
    "ingestion_job": "SELECT state, local_reservation_id, owner_user_id,"
                     " owner_role,"
                     " (commit_intent_json IS NOT NULL) AS has_intent FROM"
                     " ingestion_jobs WHERE job_id=%s",
    "baidu_batch": "SELECT state, quota_reservation_id, owner_user_id FROM"
                   " baidu_import_batches WHERE id=%s",
    "producer_import": "SELECT state, final_reservation_id,"
                       " scratch_reservation_id, owner_user_id FROM"
                       " producer_imports WHERE import_id=%s",
}
_RESERV_SQL = ("SELECT user_id, state, reserved_bytes, holder_kind,"
               " holder_id, purpose FROM upload_reservations WHERE"
               " reservation_id=%s")


class EvidenceError(Exception):
    """证据不完整/漂移——no-go（不能低报为 0 或忽略）。"""


def _connect(database_url=None):
    conn = psycopg.connect(database_url or os.environ.get("DATABASE_URL"))
    conn.row_factory = psycopg.rows.dict_row
    return conn


# --------------------------------------------------------------------------- #
# 文件证据（§4.1 + R13-3：逐成员清单 + 硬链接计量 + 越界拒绝；错误=no-go
# 不是 0——目录符号链接/枚举失败/读取失败显式阻断，不把少扫描当没有数据）
# --------------------------------------------------------------------------- #
def _walk_onerror(exc):
    raise EvidenceError("暂存目录枚举失败：%s" % exc)


def _sha256_path(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_task_manifest(upload_dir, task_id):
    """任务暂存树逐成员证据清单：``([{"path","size","sha256"}], 文件数,
    总字节)``。

    path 为相对任务暂存根的 posix 路径（排序确定）；sha256 为成员内容
    哈希（同 inode 硬链接复用）。硬链接同 (st_dev, st_ino) 只计一次
    （files/bytes 计量），路径全部列出。

    拒绝（EvidenceError）：任务根非目录/符号链接、成员符号链接（文件或
    目录）、非常规文件、目录枚举/成员读取失败。树不存在 = 空清单。"""
    base = slide_storage.staging_task_dir(str(task_id), root=upload_dir)
    if not os.path.exists(base):
        return [], 0, 0
    if base.is_symlink() or not base.is_dir():
        raise EvidenceError("任务暂存路径不是目录：%s" % base)
    manifest, seen, digests = [], set(), {}
    nfiles = total = 0
    for root, dirs, names in os.walk(base, followlinks=False,
                                     onerror=_walk_onerror):
        keep = []
        for d in sorted(dirs):
            dp = os.path.join(root, d)
            if os.path.islink(dp):
                raise EvidenceError("暂存树含目录符号链接（拒绝）：%s" % dp)
            keep.append(d)
        dirs[:] = keep
        for name in sorted(names):
            p = os.path.join(root, name)
            if os.path.islink(p):
                raise EvidenceError("暂存树含符号链接（拒绝）：%s" % p)
            try:
                st = os.stat(p)
            except OSError as exc:
                raise EvidenceError("暂存成员无法读取：%s（%s）" % (p, exc))
            if not _stat.S_ISREG(st.st_mode):
                raise EvidenceError("暂存成员不是常规文件：%s" % p)
            key = (st.st_dev, st.st_ino)
            digest = digests.get(key)
            if digest is None:
                try:
                    digest = _sha256_path(p)
                except OSError as exc:
                    raise EvidenceError("暂存成员读取失败：%s（%s）"
                                        % (p, exc))
                digests[key] = digest
            rel = os.path.relpath(p, base).replace(os.sep, "/")
            manifest.append({"path": rel, "size": int(st.st_size),
                             "sha256": digest})
            if key not in seen:
                seen.add(key)
                total += int(st.st_size)
                nfiles += 1
    manifest.sort(key=lambda e: (e["path"], e["size"], e["sha256"]))
    return manifest, nfiles, total


def scan_task_tree(upload_dir, task_id):
    """(文件数, 总字节)——兼容包装；证据核验用 scan_task_manifest。"""
    _, nfiles, total = scan_task_manifest(upload_dir, task_id)
    return nfiles, total


# --------------------------------------------------------------------------- #
# 扫描集合（§4.1：任务表/预约表/清理表/存储目录双向核对）
# --------------------------------------------------------------------------- #
def collect(cur, upload_dir):
    cur.execute(
        "SELECT t.upload_id, t.owner_user_id, t.state, t.reservation_id AS"
        " rid, t.commit_intent_json, t.commit_token, t.quota_mode,"
        " u.role AS owner_role, r.state AS rstate,"
        " r.user_id AS ruser, r.reserved_bytes,"
        " r.holder_kind, r.holder_id FROM upload_tasks t"
        " LEFT JOIN upload_reservations r ON r.reservation_id=t.reservation_id"
        " LEFT JOIN users u ON u.user_id=t.owner_user_id"
        " ORDER BY t.upload_id")
    tasks = [dict(r) for r in cur.fetchall()]

    cur.execute(
        "SELECT j.job_id, j.owner_user_id, j.owner_role, j.state,"
        " j.local_reservation_id AS rid, j.local_cleanup_status,"
        " j.commit_intent_json, r.state AS rstate, r.user_id AS ruser,"
        " r.reserved_bytes, r.holder_kind, r.holder_id FROM ingestion_jobs j"
        " LEFT JOIN upload_reservations r ON r.reservation_id="
        " j.local_reservation_id ORDER BY j.job_id")
    jobs = [dict(r) for r in cur.fetchall()]

    cur.execute(
        "SELECT b.id AS batch_id, b.owner_user_id, b.state,"
        " b.quota_reservation_id AS rid, b.total_bytes, r.state AS rstate,"
        " r.user_id AS ruser, r.reserved_bytes, r.holder_kind, r.holder_id"
        " FROM baidu_import_batches b"
        " LEFT JOIN upload_reservations r ON r.reservation_id="
        " b.quota_reservation_id ORDER BY b.id")
    batches = [dict(r) for r in cur.fetchall()]

    # C5 producer 导入（双预约：final/scratch 各一份；豁免身份两者皆空）
    cur.execute(
        "SELECT p.import_id, p.owner_user_id, p.state,"
        " p.final_reservation_id AS final_rid,"
        " p.scratch_reservation_id AS scratch_rid,"
        " (p.commit_intent_json IS NOT NULL) AS has_intent,"
        " u.role AS owner_role,"
        " fr.state AS final_rstate, fr.user_id AS final_ruser,"
        " fr.reserved_bytes AS final_reserved, fr.holder_kind AS"
        " final_holder_kind, fr.holder_id AS final_holder_id,"
        " fr.purpose AS final_purpose,"
        " sr.state AS scratch_rstate, sr.user_id AS scratch_ruser,"
        " sr.reserved_bytes AS scratch_reserved, sr.holder_kind AS"
        " scratch_holder_kind, sr.holder_id AS scratch_holder_id,"
        " sr.purpose AS scratch_purpose"
        " FROM producer_imports p"
        " LEFT JOIN upload_reservations fr ON fr.reservation_id="
        " p.final_reservation_id"
        " LEFT JOIN upload_reservations sr ON sr.reservation_id="
        " p.scratch_reservation_id"
        " LEFT JOIN users u ON u.user_id=p.owner_user_id"
        " WHERE p.state <> 'done' ORDER BY p.import_id")
    producers = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT upload_id, reservation_id, attempts FROM "
                "upload_cleanup_pending ORDER BY upload_id")
    pending_rows = [dict(r) for r in cur.fetchall()]
    pending_by_task = {p["upload_id"]: p for p in pending_rows}

    items = []
    deferred = []  # R14-1：已知终态但无 rid/pending 者——扫描后再决定入集
    for t in tasks:
        t["quota_duty"] = _upload_quota_duty(t)
        duty = t["quota_duty"]
        suffix = "" if duty else ("_exempt" if duty is False else "")
        if t["state"] in _ACTIVE_UPLOAD_TASK_STATES:
            items.append(("upload_task" + suffix, t))
        elif t["rid"] or t["upload_id"] in pending_by_task:
            row = dict(t)
            row["pending"] = pending_by_task.get(t["upload_id"])
            items.append(("upload_task_terminal" + suffix, row))
        else:
            row = dict(t)
            row["pending"] = None
            deferred.append(("upload_task_terminal" + suffix, row))
    for j in jobs:
        duty = (j["owner_role"] == "user" and (j["owner_user_id"] or ""))
        if j["state"] in _ACTIVE_INGESTION_STATES:
            items.append(("ingestion_job" if duty
                          else "ingestion_job_exempt", j))
        elif j["local_cleanup_status"] in ("pending", "failed"):
            items.append(("ingestion_job_cleanup", j))
        else:
            deferred.append(("ingestion_job_cleanup" if duty
                             else "ingestion_job_exempt", j))
    for b in batches:
        if b["state"] in ("queued", "running"):
            items.append(("baidu_batch", b))
    for p in producers:
        # 豁免身份（owner/本地免登录）与 ingestion 同口径：role=user 才有
        # 配额责任；两份预约皆空按身份合同识别，非异常。
        duty = (p["owner_role"] == "user" and (p["owner_user_id"] or ""))
        items.append(("producer_import" if duty else "producer_import_exempt",
                      p))

    referenced = set()
    for t in tasks:
        if t["rid"]:
            referenced.add(t["rid"])
    for j in jobs:
        if j["rid"]:
            referenced.add(j["rid"])
    for b in batches:
        if b["rid"]:
            referenced.add(b["rid"])
    for p in producers:
        if p["final_rid"]:
            referenced.add(p["final_rid"])
        if p["scratch_rid"]:
            referenced.add(p["scratch_rid"])
    for p in pending_rows:
        if p["reservation_id"]:
            referenced.add(p["reservation_id"])
    cur.execute(
        "SELECT reservation_id, user_id, reserved_bytes, holder_kind,"
        " holder_id FROM upload_reservations WHERE state='reserved'"
        " ORDER BY reservation_id")
    dangling = [dict(r) for r in cur.fetchall()
                if r["reservation_id"] not in referenced]

    known_ids = {t["upload_id"] for t in tasks} | {j["job_id"] for j in jobs} \
        | {p["import_id"] for p in producers}
    unknown_dirs = []
    sroot = os.path.join(str(upload_dir), ".staging")
    if os.path.isdir(sroot):
        for name in sorted(os.listdir(sroot)):
            if name not in known_ids:
                unknown_dirs.append(name)

    cur.execute(
        "SELECT user_id, quota_bytes AS base_quota_bytes,"
        " extra_quota_bytes, quota_bytes + extra_quota_bytes AS quota_bytes,"
        " used_bytes, reserved_bytes,"
        " used_bytes + reserved_bytes - quota_bytes - extra_quota_bytes"
        " AS over FROM upload_user_quotas WHERE used_bytes + reserved_bytes >"
        " quota_bytes + extra_quota_bytes ORDER BY user_id")
    over_quota = [dict(r) for r in cur.fetchall()]

    # R14-2：逐用户双向核对账本——quota.reserved_bytes 必须等于该用户
    # state='reserved' 预约合计（reservation_holds_capacity 语义；admission
    # 与 reconcile 来源同账）。多记/少记/缺配额行均为不可解释差额。
    cur.execute(
        "SELECT COALESCE(q.user_id, r.user_id) AS user_id,"
        " (MAX(q.user_id) IS NOT NULL) AS has_quota_row,"
        " MAX(q.reserved_bytes) AS quota_reserved,"
        " COALESCE(SUM(CASE WHEN r.state='reserved' THEN r.reserved_bytes"
        " ELSE 0 END), 0)::bigint AS sum_reserved FROM upload_user_quotas q"
        " FULL JOIN upload_reservations r ON r.user_id=q.user_id"
        " GROUP BY 1 ORDER BY 1")
    quota_drift = []
    for r in cur.fetchall():
        row = dict(r)
        quota_r = int(row["quota_reserved"] or 0)
        sum_r = int(row["sum_reserved"] or 0)
        if quota_r == sum_r:
            continue
        quota_drift.append({
            "user_id": row["user_id"],
            "reason": ("quota_row_missing" if not row["has_quota_row"]
                       else ("ledger_over" if quota_r > sum_r
                             else "ledger_under")),
            "quota_reserved_bytes": quota_r,
            "reservation_sum_bytes": sum_r})

    # 逐项字节证据（豁免身份无需——预约为空按身份合同识别，非异常）
    for kind, row in items:
        if kind.endswith("_exempt"):
            continue
        base_kind = _base_kind(kind)
        tid = row[_HOLDER_ID_KEY[base_kind]]
        (row["staging_manifest"], row["staging_files"],
         row["staging_bytes"]) = scan_task_manifest(upload_dir, tid)
    # R14-1：已知终态但无 rid/pending 的任务/作业——暂存树非空即入审计
    # 集合（「存在任务行」只确定归属候选，不是免检证据）；空目录=无残留。
    for kind, row in deferred:
        if kind.endswith("_exempt"):
            continue
        base_kind = _base_kind(kind)
        tid = row[_HOLDER_ID_KEY[base_kind]]
        manifest, nfiles, total = scan_task_manifest(upload_dir, tid)
        if not manifest:
            continue
        row["staging_manifest"] = manifest
        row["staging_files"] = nfiles
        row["staging_bytes"] = total
        items.append((kind, row))
    return {"items": items, "pending": pending_rows, "dangling": dangling,
            "unknown_dirs": unknown_dirs, "over_quota": over_quota,
            "quota_drift": quota_drift,
            "counts": {"upload_tasks": len(tasks),
                       "ingestion_jobs": len(jobs),
                       "baidu_import_batches": len(batches),
                       "producer_imports": len(producers),
                       "upload_cleanup_pending": len(pending_rows)}}


def _base_kind(kind):
    return kind.replace("_terminal", "").replace("_cleanup", "").replace(
        "_exempt", "")


def classify_producer(row):
    """producer_import 双预约核验：final/scratch 各自独立判定。

    返回 "ok"（全部在场预约 reserved + holder/owner/用途一致）或首个异常
    verdict（missing/released/consumed/mismatch_holder/mismatch_owner/
    mismatch_purpose——与 classify 的词表兼容，供 plan_actions 阻断）。"""
    verdict = "ok"
    for prefix in ("final", "scratch"):
        rid = row.get("%s_rid" % prefix)
        if not rid:
            continue  # scratch 可缺省（begin 申报 0 / 豁免身份）
        if row.get("%s_rstate" % prefix) is None:
            return "missing"
        if row["%s_rstate" % prefix] != "reserved":
            return row["%s_rstate" % prefix]
        if (row["%s_holder_id" % prefix] or None) != row["import_id"] or \
                (row["%s_holder_kind" % prefix] or None) != "producer_import":
            if row["%s_holder_id" % prefix]:
                return "mismatch_holder"
            return "bind"
        if row.get("%s_purpose" % prefix) not in \
                _KIND_PURPOSE_SETS["producer_import"]:
            return "mismatch_purpose"
        if row["%s_ruser" % prefix] != row.get("owner_user_id"):
            return "mismatch_owner"
    return verdict


def _upload_quota_duty(row):
    """R15：上传任务配额身份合同（与 upload_guard.quota_applies 同语义）。

    创建时快照（0074 quota_mode）优先——角色事后经 SQL 变更不影响历史
    裁决；存量 NULL 按当前 users.role：空 owner=本地免登录豁免；非 user
    角色豁免；无用户行（非空 owner）= 不可证明 → None（核账阻断人工核
    对，不自动终止）。"""
    qm = row.get("quota_mode")
    if qm in ("duty", "exempt"):
        return qm == "duty"
    if not (row.get("owner_user_id") or ""):
        return False  # 本地免登录 owner（0017 合同：空 user_id）
    role = row.get("owner_role")
    if role is None:
        return None  # 非空 owner 但无用户行：身份不可证明
    return role == "user"


def classify(kind, row):
    """(kind,row) → ok/bind/missing/released/consumed/mismatch_*。"""
    if kind.endswith("_exempt"):
        return "ok"
    base_kind = _base_kind(kind)
    rid = row.get("rid") or (row.get("pending") or {}).get("reservation_id")
    if not rid:
        return "missing"
    if row.get("rstate") is None:
        return "missing"
    if row["rstate"] != "reserved":
        return row["rstate"]
    holder_id = row[_HOLDER_ID_KEY[base_kind]]
    if (row.get("holder_id") or None) == holder_id and \
            (row.get("holder_kind") or None) == base_kind:
        if row.get("ruser") == row.get("owner_user_id"):
            return "ok"
        return "mismatch_owner"
    if row.get("holder_id"):
        return "mismatch_holder"
    if row.get("ruser") != row.get("owner_user_id"):
        return "mismatch_owner"
    return "bind"


def _commit_intent_open(kind, row):
    """R14-3：未解决的提交意图——核账不得自动降级/排清理。

    upload_task：已持久化 commit_intent_json / commit_token，或 state=
    committing（发布临界态，清理会销毁恢复所需文件）。终态任务不在此列
    （committed 历史行的 intent json 按合同长期保留，属正常痕迹）。
    ingestion_job：已持久化 intent，或 state∈{completing, validating}
    （验证/发布临界段）。producer_import：已持久化 intent 或 state=
    committing（同步提交段——恢复路径重跑 publish 收口，不得 stop/repair）。"""
    if kind == "upload_task":
        return bool(row.get("commit_intent_json")) or \
            bool(row.get("commit_token")) or \
            row.get("state") == "committing"
    if kind == "ingestion_job":
        return bool(row.get("commit_intent_json")) or \
            row.get("state") in ("completing", "validating")
    if kind == "producer_import":
        return bool(row.get("has_intent")) or \
            row.get("state") == "committing"
    return False


def plan_actions(state, repair_residuals):
    """扫描态 → (actions, blockers)。动作 key 持久唯一、可重放。

    R13-2：consumed（任何任务状态）一律 blocker——字节已结算进 used，
    暂存残留与已发布对象/已结算源的资产关系（scan 的 inode 去重只在单
    任务树内）无法在核账内证明独立，自动补 reserved 等于重复收费。"""
    actions, blockers = [], []
    for kind, row in state["items"]:
        base_kind = _base_kind(kind)
        tid = row[_HOLDER_ID_KEY[base_kind]]
        rid = row.get("rid") or (row.get("pending") or
                                {}).get("reservation_id")
        if base_kind == "producer_import":
            # C5：双预约逐份核验（classify_producer）；producer 持有者不自动
            # stop/repair——committing/intent 是未裁决提交段（恢复路径收口），
            # 取消/清理走通道自身编排；任何非 ok verdict 一律阻断人工核对。
            verdict = classify_producer(row)
            if verdict == "ok":
                continue
            blockers.append({
                "kind": kind, "id": tid,
                "reason": ("commit_intent_unresolved"
                           if _commit_intent_open(base_kind, row)
                           else "producer_reservation_%s" % verdict),
                "state": row.get("state"),
                "observed": verdict,
                "reservation_id": row.get("final_rid")
                or row.get("scratch_rid")})
            continue
        verdict = classify(kind, row)
        if kind.startswith("upload_task") and \
                row.get("quota_duty") is None:
            # R15-1：非空 owner 无用户行——身份不可证明，不能凭 rid 缺失
            # 推断 duty/豁免 → 阻断人工核对，不自动终止。
            blockers.append({"kind": kind, "id": tid,
                             "reason": "identity_unresolvable",
                             "owner": row.get("owner_user_id")})
            continue
        if verdict == "ok":
            continue
        if verdict == "bind":
            if kind == "upload_task_terminal" and \
                    int(row.get("staging_bytes") or 0) > \
                    int(row.get("reserved_bytes") or 0):
                blockers.append({"kind": kind, "id": tid,
                                 "reason": "residue_exceeds_reservation",
                                 "bytes": row.get("staging_bytes"),
                                 "reserved": row.get("reserved_bytes")})
                continue
            actions.append({
                "action_key": "bind:%s:%s:%s" % (base_kind, tid, rid),
                "action": "bind", "kind": base_kind, "id": tid,
                "reservation_id": rid,
                "evidence": {"bytes": row.get("staging_bytes"),
                             "reserved_bytes": row.get("reserved_bytes"),
                             "manifest": row.get("staging_manifest")}})
            continue
        if verdict in ("missing", "released", "consumed"):
            has_residue = int(row.get("staging_bytes") or 0) > 0
            if kind in ("upload_task", "ingestion_job") and \
                    _commit_intent_open(kind, row):
                # R14-3：提交意图未裁决（发布可能只进行到一半）——禁止
                # stop/repair，不得把恢复所需文件交给清理器；由独立提交
                # 恢复/对账流程确认状态后再收口。
                blockers.append({"kind": kind, "id": tid,
                                 "reason": "commit_intent_unresolved",
                                 "state": row.get("state"),
                                 "reservation_id": rid,
                                 "bytes": row.get("staging_bytes")})
                continue
            if kind in ("upload_task", "ingestion_job"):
                actions.append({
                    "action_key": "stop:%s:%s" % (base_kind, tid),
                    "action": "stop", "kind": base_kind, "id": tid,
                    "observed": verdict,
                    "evidence": {"bytes": row.get("staging_bytes"),
                                 "manifest": row.get("staging_manifest")}})
                if verdict == "consumed":
                    blockers.append({"kind": kind, "id": tid,
                                     "reason": "consumed_unexplained",
                                     "reservation_id": rid})
                elif repair_residuals and has_residue:
                    actions.append(_repair_action(base_kind, tid, row))
                elif has_residue:
                    blockers.append({"kind": kind, "id": tid,
                                     "reason": "residual_without_duty",
                                     "bytes": row.get("staging_bytes"),
                                     "need_flag": "--repair-residuals"})
            elif kind in ("upload_task_terminal", "ingestion_job_cleanup"):
                if verdict == "consumed":
                    if has_residue:
                        # R13-2：已结算仍残留——资产关系不可证明独立，人工
                        # 核对。无残留的 consumed（正常 committed 历史）不
                        # 阻断（R14 矩阵：字节与文件都已收口）。
                        blockers.append({"kind": kind, "id": tid,
                                         "reason": "consumed_unexplained",
                                         "reservation_id": rid,
                                         "bytes": row.get("staging_bytes")})
                elif repair_residuals and has_residue:
                    actions.append(_repair_action(base_kind, tid, row))
                elif has_residue:
                    blockers.append({"kind": kind, "id": tid,
                                     "reason": "residual_without_duty",
                                     "bytes": row.get("staging_bytes"),
                                     "need_flag": "--repair-residuals"})
            elif kind == "baidu_batch":
                blockers.append({"kind": kind, "id": tid,
                                 "reason": "shared_budget_unattributable",
                                 "observed": verdict})
            continue
        blockers.append({"kind": kind, "id": tid, "reason": verdict,
                         "reservation_id": rid})

    for r in state["dangling"]:
        blockers.append({"kind": "reservation", "id": r["reservation_id"],
                         "reason": "dangling_reserved",
                         "user": r["user_id"],
                         "reserved_bytes": int(r["reserved_bytes"]),
                         "holder": [r["holder_kind"], r["holder_id"]]})
    for d in state["unknown_dirs"]:
        blockers.append({"kind": "staging_dir", "id": d,
                         "reason": "unknown_staging_dir"})
    for q in state.get("quota_drift") or []:
        blockers.append({"kind": "quota_ledger", "id": q["user_id"],
                         "reason": q["reason"],
                         "quota_reserved_bytes": q["quota_reserved_bytes"],
                         "reservation_sum_bytes": q["reservation_sum_bytes"]})
    return actions, blockers


def _repair_action(base_kind, tid, row):
    return {"action_key": "repair:%s:%s" % (base_kind, tid),
            "action": "repair", "kind": base_kind, "id": tid,
            "owner": row.get("owner_user_id"),
            "evidence": {"bytes": row.get("staging_bytes"),
                         "files": row.get("staging_files"),
                         "manifest": row.get("staging_manifest")}}


def _prestate_for(cur, actions):
    """冻结/复核用的 DB 前态：全局计数 + pending 清单 + 逐动作目标行
    （含 owner 等裁决字段）+ 逐动作预约行（owner/state/holder/purpose/
    金额）。"""
    pre = {"counts": {}, "pending_ids": [], "targets": {}, "reservations": {}}
    for table in ("upload_tasks", "ingestion_jobs", "baidu_import_batches",
                  "producer_imports", "upload_cleanup_pending",
                  "upload_reservations"):
        cur.execute("SELECT COUNT(*)::int AS n FROM %s" % table)
        pre["counts"][table] = cur.fetchone()["n"]
    cur.execute("SELECT upload_id FROM upload_cleanup_pending"
                " ORDER BY upload_id")
    pre["pending_ids"] = [r["upload_id"] for r in cur.fetchall()]
    for act in actions:
        key = act["action_key"]
        cur.execute(_TARGET_SQL[act["kind"]], (act["id"],))
        row = cur.fetchone()
        pre["targets"][key] = row and dict(row) or None
        if act.get("reservation_id"):
            cur.execute(_RESERV_SQL, (act["reservation_id"],))
            rrow = cur.fetchone()
            pre["reservations"][key] = rrow and dict(rrow) or None
    return pre


def _receipt_rows(cur, plan):
    cur.execute("SELECT action_key, result_reservation_id FROM "
                "upload_capacity_repair_receipts WHERE plan_hash=%s",
                (plan["plan_hash"],))
    return cur.fetchall()


def _applied_action_keys(cur, plan):
    return {r["action_key"] for r in _receipt_rows(cur, plan)}


def _verify_prestate(cur, plan, already_applied=frozenset(),
                     for_update=False):
    """DB 前态复核（R13-1：裁决字段级，非只行数）。

    已应用动作的目标跳过（后继由 _verify_successors 核验）。全局计数：
    本工具不删行、repair 恰 +1 行预约——除已应用 repair 的 +delta 外必须
    精确相等；upload_cleanup_pending 允许两类合法变化：基线行的确认清理
    删除、已应用 stop 的 upsert 新增，出现计划外行即漂移。
    for_update=True 时目标/预约行 SELECT ... FOR UPDATE（应用事务内的
    锁定前态再判定）。"""
    expect = plan["db_prestate"]
    suffix = " FOR UPDATE" if for_update else ""
    delta_reservations = sum(1 for a in plan["actions"]
                             if a["action_key"] in already_applied
                             and a["action"] == "repair")
    for table in ("upload_tasks", "ingestion_jobs", "baidu_import_batches",
                  "producer_imports", "upload_reservations"):
        want = int(expect["counts"].get(table, 0))
        if table == "upload_reservations":
            want += delta_reservations
        cur.execute("SELECT COUNT(*)::int AS n FROM %s" % table)
        got = cur.fetchone()["n"]
        if got != want:
            raise EvidenceError("表 %s 计数漂移（期望 %d 实际 %d）"
                                % (table, want, got))
    cur.execute("SELECT upload_id FROM upload_cleanup_pending")
    now_pending = {r["upload_id"] for r in cur.fetchall()}
    stop_targets = {a["id"] for a in plan["actions"]
                    if a["action"] == "stop" and a["kind"] == "upload_task"
                    and a["action_key"] in already_applied}
    extra = now_pending - set(expect.get("pending_ids") or []) - stop_targets
    if extra:
        raise EvidenceError("upload_cleanup_pending 计划外新增（环境漂移）："
                            "%s" % sorted(extra))
    for act in plan["actions"]:
        key = act["action_key"]
        if key in already_applied:
            continue  # 已应用：后继关系由 _verify_successors 覆盖
        cur.execute(_TARGET_SQL[act["kind"]] + suffix, (act["id"],))
        row = cur.fetchone()
        cur_row = row and dict(row) or None
        exp = (expect["targets"] or {}).get(key)
        if cur_row != exp:
            raise EvidenceError("目标前态漂移：%s（%r → %r）"
                                % (key, exp, cur_row))
        if act.get("reservation_id"):
            cur.execute(_RESERV_SQL + suffix, (act["reservation_id"],))
            rrow = cur.fetchone()
            cur_r = rrow and dict(rrow) or None
            exp_r = (expect["reservations"] or {}).get(key)
            if cur_r != exp_r:
                raise EvidenceError("预约前态漂移：%s（%r → %r）"
                                    % (key, exp_r, cur_r))


def _require_bound_or_released(cur, act, rid, residue_present,
                               residue_lost, owner=None, nbytes=None):
    """bind/repair 已应用动作的预约后继：仍持有（reserved+绑定一致）或
    已合法释放（且残留确已不在）。其余形态一律漂移。

    residue_lost 仅在**冻结时曾有残留**而当前已消失时为真（冻结时本就
    无残留的任务，空清单不代表清理发生过）。"""
    cur.execute(_RESERV_SQL, (rid,))
    row = cur.fetchone()
    if row is None:
        raise EvidenceError("回执预约不存在（%s/%s）" % (act["action_key"], rid))
    if row["state"] == "released":
        if residue_present:
            raise EvidenceError("预约已释放但暂存残留仍在（清理未完成/漂"
                                "移）：%s" % act["action_key"])
        return
    if row["state"] != "reserved" or \
            (row["holder_kind"] or None) != act["kind"] or \
            (row["holder_id"] or None) != act["id"] or \
            (row["purpose"] or None) != _PURPOSE[act["kind"]]:
        raise EvidenceError("回执预约绑定后继不符：%s（%r）"
                            % (act["action_key"], dict(row)))
    if owner is not None and row["user_id"] != owner:
        raise EvidenceError("回执预约 owner 后继不符：%s（%r != %r）"
                            % (act["action_key"], row["user_id"], owner))
    if nbytes is not None and int(row["reserved_bytes"]) != int(nbytes):
        raise EvidenceError("回执预约金额后继不符：%s（%s != %s）"
                            % (act["action_key"], row["reserved_bytes"],
                               nbytes))
    if residue_lost:
        raise EvidenceError("冻结时的暂存残留已消失但预约仍持有（未收口/"
                            "漂移）：%s" % act["action_key"])


def _require_terminal(cur, act):
    if act["kind"] == "upload_task":
        cur.execute("SELECT state FROM upload_tasks WHERE upload_id=%s",
                    (act["id"],))
        actives = _ACTIVE_UPLOAD_TASK_STATES
    elif act["kind"] == "ingestion_job":
        cur.execute("SELECT state FROM ingestion_jobs WHERE job_id=%s",
                    (act["id"],))
        actives = _ACTIVE_INGESTION_STATES
    else:
        return
    row = cur.fetchone()
    if row is None:
        raise EvidenceError("stop 已应用但目标消失：%s" % act["action_key"])
    if row["state"] in actives:
        raise EvidenceError("stop 已应用但目标仍活跃：%s（state=%s）"
                            % (act["action_key"], row["state"]))


def _verify_successors(cur, upload_dir, plan, already_applied):
    """已应用动作的合法后继核验（R13-4：回执优先，清理后重跑非漂移）。

    三类合法生命周期：未执行（不在回执，走原前态核验）、已执行仍持有
    （残留清单与冻结一致 + 预约 reserved 绑定一致）、已合法结算/清理
    （残留已清 + 预约 released / stop 目标终态）。既非持有又非合法结算
    （如残留消失但预约仍 reserved、预约释放但残留仍在、清单内容漂移）
    一律阻断。"""
    if not already_applied:
        return
    receipts = {r["action_key"]: dict(r) for r in _receipt_rows(cur, plan)}
    for act in plan["actions"]:
        key = act["action_key"]
        if key not in already_applied:
            continue
        manifest, _, _ = scan_task_manifest(upload_dir, act["id"])
        frozen = (act.get("evidence") or {}).get("manifest") or []
        if manifest and manifest != frozen:
            raise EvidenceError("已应用动作的残留清单漂移：%s" % key)
        residue_present = bool(manifest)
        residue_lost = bool(frozen) and not manifest
        if act["action"] == "stop":
            _require_terminal(cur, act)
        elif act["action"] == "bind":
            _require_bound_or_released(cur, act, act["reservation_id"],
                                       residue_present, residue_lost)
        elif act["action"] == "repair":
            rid = (receipts.get(key) or {}).get("result_reservation_id")
            if not rid:
                raise EvidenceError("repair 已应用但回执未记录新预约：%s"
                                    % key)
            _require_bound_or_released(
                cur, act, rid, residue_present, residue_lost,
                owner=act.get("owner"),
                nbytes=(act.get("evidence") or {}).get("bytes"))


def _verify_file_evidence(upload_dir, plan, already_applied=frozenset()):
    """未应用动作的文件证据：逐成员路径/类型/size/sha256 精确一致
    （R13-3：同数量等大小内容替换/改名可检出）。"""
    for act in plan["actions"]:
        if act["action_key"] in already_applied:
            continue
        frozen = (act.get("evidence") or {}).get("manifest")
        if frozen is None:
            continue
        manifest, _, _ = scan_task_manifest(upload_dir, act["id"])
        if manifest != frozen:
            raise EvidenceError("文件证据漂移：%s（冻结 %d 成员 → 当前 %d "
                                "成员）" % (act["action_key"], len(frozen),
                                           len(manifest)))


def apply_action(cur, act):
    kind, tid = act["kind"], act["id"]
    if act["action"] == "bind":
        upload_guard.bind_reservation_locked(
            cur, act["reservation_id"], kind, tid, _PURPOSE[kind])
        return {"bound": act["reservation_id"]}
    if act["action"] == "stop":
        if kind == "upload_task":
            cur.execute(
                "UPDATE upload_tasks SET state='failed', updated_at=now()"
                " WHERE upload_id=%s AND state = ANY(%s)",
                (tid, list(_ACTIVE_UPLOAD_TASK_STATES)))
            cur.execute(
                "INSERT INTO upload_cleanup_pending (upload_id,"
                " reservation_id, attempts, last_error)"
                " VALUES (%s,NULL,1,%s) ON CONFLICT (upload_id) DO UPDATE"
                " SET attempts = upload_cleanup_pending.attempts + 1,"
                " last_error = EXCLUDED.last_error, updated_at=now()",
                (tid, "reconcile: reservation %s" % act.get("observed")))
        else:
            cur.execute(
                "UPDATE ingestion_jobs SET state='failed', fail_code="
                "'local_reservation_invalid', terminal_at=now(),"
                " cleanup_status='pending', local_cleanup_status='pending',"
                " updated_at=now() WHERE job_id=%s AND state = ANY(%s)",
                (tid, list(_ACTIVE_INGESTION_STATES)))
        return {"stopped": True}
    if act["action"] == "repair":
        res = upload_guard.record_reconciled_residual_locked(
            cur, act["owner"], int(act["evidence"]["bytes"]),
            holder_kind=kind, holder_id=tid, purpose=_PURPOSE[kind])
        if kind == "upload_task":
            cur.execute("UPDATE upload_tasks SET reservation_id=%s,"
                        " updated_at=now() WHERE upload_id=%s",
                        (res["reservation_id"], tid))
            # R15-2：责任 + 绑定 + 持久清理工作同一事务齐全——终态补记
            # 未必有前置 stop 建行（R14 发现的终态无 rid/pending 残留），
            # 幂等 upsert 保证清理器可领取。
            cur.execute(
                "INSERT INTO upload_cleanup_pending (upload_id,"
                " reservation_id, attempts, last_error)"
                " VALUES (%s, %s, 1, 'reconcile: residual duty re-attached')"
                " ON CONFLICT (upload_id) DO UPDATE SET"
                " reservation_id = EXCLUDED.reservation_id,"
                " updated_at = now()",
                (tid, res["reservation_id"]))
        else:
            # COS：本地责任绑定 + 清理工作重置为可领取（none/cleaned →
            # pending；pending/failed 原样保留重试资格；不动远端清理结果）。
            cur.execute(
                "UPDATE ingestion_jobs SET local_reservation_id=%s,"
                " local_cleanup_status = CASE WHEN local_cleanup_status IN"
                " ('pending','failed') THEN local_cleanup_status ELSE"
                " 'pending' END,"
                " updated_at=now() WHERE job_id=%s",
                (res["reservation_id"], tid))
        return {"repaired": res["reservation_id"],
                "bytes": int(act["evidence"]["bytes"])}
    raise ValueError("未知动作：%r" % act["action"])


def _unexplained_after(state, actions_left, blockers_left):
    """应用后不得为 0 的项：残留无 duty / dangling / 未知目录 / 计划外 /
    待清残留无可领取的持久清理工作（R15-2 终验）。"""
    problems = list(blockers_left)
    for a in actions_left:
        problems.append({"kind": a["kind"], "id": a["id"],
                         "reason": "action_still_pending",
                         "action_key": a["action_key"]})
    for kind, row in state["items"]:
        if kind == "upload_task_terminal" and \
                int(row.get("staging_bytes") or 0) > 0:
            pend = row.get("pending")
            if not pend or not (pend.get("reservation_id")
                                or row.get("rid")):
                problems.append({"kind": kind, "id": row.get("upload_id"),
                                 "reason": "residue_without_cleanup_work"})
        elif kind == "ingestion_job_cleanup" and \
                int(row.get("staging_bytes") or 0) > 0 and \
                row.get("local_cleanup_status") not in ("pending", "failed"):
            problems.append({"kind": kind, "id": row.get("job_id"),
                             "reason": "residue_without_cleanup_work"})
    return problems


def _plan_hash(plan):
    body = {k: v for k, v in plan.items() if k != "plan_hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _report_actions(actions):
    """报告版动作：证据里逐文件清单（sha256 数组）折叠为条目数。"""
    out = []
    for a in actions:
        a = dict(a)
        ev = a.get("evidence")
        if isinstance(ev, dict) and "manifest" in ev:
            a["evidence"] = {k: v for k, v in ev.items() if k != "manifest"}
            a["evidence"]["manifest_files"] = len(ev["manifest"])
        out.append(a)
    return out


def _report_pending_work(state):
    def _row(r):
        return {k: v for k, v in r.items()
                if k not in ("staging_manifest", "commit_intent_json",
                             "commit_token")}
    def _producer_row(r):
        return {"import_id": r.get("import_id"), "state": r.get("state"),
                "owner": r.get("owner_user_id"),
                "final_reservation_id": r.get("final_rid"),
                "scratch_reservation_id": r.get("scratch_rid"),
                "staging_files": r.get("staging_files"),
                "staging_bytes": r.get("staging_bytes")}
    return {
        "upload_cleanup_pending": state["pending"],
        "ingestion_local_cleanup": [
            _row(r) for k, r in state["items"]
            if k == "ingestion_job_cleanup"],
        "producer_import_cleanup": [
            _producer_row(r) for k, r in state["items"]
            if k.startswith("producer_import")],
    }


def _residual_without_duty(state, actions=()):
    planned_duty = {a["id"] for a in actions if a["action"] == "repair"}
    for kind, row in state["items"]:
        if kind.endswith("_exempt"):
            continue
        base_kind = _base_kind(kind)
        if row.get(_HOLDER_ID_KEY[base_kind]) in planned_duty:
            continue  # 计划内补记责任（--repair-residuals）
        if base_kind == "producer_import":
            # producer：双预约核验（classify_producer）；无预约列（rid）可读，
            # 责任判定以 verdict 为准——豁免身份已被 _exempt 跳过。
            if int(row.get("staging_bytes") or 0) > 0 and \
                    classify_producer(row) != "ok":
                return True
            continue
        if int(row.get("staging_bytes") or 0) > 0 and \
                classify(kind, row) != "ok" and not (
                    row.get("rid")
                    or (row.get("pending") or {}).get("reservation_id")):
            return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--upload-dir", default=os.environ.get("UPLOAD_DIR"))
    ap.add_argument("--apply", action="store_true",
                    help="应用冻结计划（必须与 --plan 同用）")
    ap.add_argument("--plan", default=None, help="冻结计划文件（应用输入）")
    ap.add_argument("--plan-out", default=None,
                    help="生成冻结计划文件（维护窗口内执行）")
    ap.add_argument("--repair-residuals", action="store_true",
                    help="按冻结证据补记残留责任（维护专用；不恢复执行）")
    ap.add_argument("--reattach", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)
    if args.reattach:
        print("--reattach 已按 R12 §3.4 拆除（核账只补责任，不恢复执行）；"
              "改用 --repair-residuals", file=sys.stderr)
        return 2
    if args.apply and not args.plan:
        print("--apply 必须与 --plan <冻结计划> 同用（在线 dry-run 不是"
              "可应用计划）", file=sys.stderr)
        return 2
    if args.upload_dir is None:
        print("--upload-dir 或 UPLOAD_DIR 必须提供（暂存字节审计输入）",
              file=sys.stderr)
        return 2
    upload_dir = os.path.abspath(str(args.upload_dir))

    conn = _connect(args.database_url)
    try:
        if args.apply:
            with open(args.plan, "r", encoding="utf-8") as fh:
                plan = json.load(fh)
            if _plan_hash(plan) != plan.get("plan_hash"):
                print("计划自校验失败（plan_hash 不符）", file=sys.stderr)
                return 3
            if plan.get("tool_version") != TOOL_VERSION or \
                    int(plan.get("schema_version") or 0) != SCHEMA_VERSION:
                print("计划由不同工具版本生成（计划 %r/%r，本工具 %r/%r）："
                      "证据/前态合同已变更，需在维护窗口重新冻结"
                      % (plan.get("tool_version"),
                         plan.get("schema_version"), TOOL_VERSION,
                         SCHEMA_VERSION), file=sys.stderr)
                return 3
            if plan.get("upload_root") != upload_dir:
                print("计划数据根与当前 --upload-dir 不符（%r != %r）"
                      % (plan.get("upload_root"), upload_dir),
                      file=sys.stderr)
                return 3
            if bool(args.repair_residuals) != bool(
                    plan.get("repair_residuals")):
                print("--repair-residuals 与冻结计划不一致（计划 %r）：应"
                      "用语义会改变，拒绝"
                      % bool(plan.get("repair_residuals")), file=sys.stderr)
                return 2
            # R13-1/R13-4：预检与应用同一事务——回执优先识别已应用动作及
            # 其合法后继（清理后重跑非漂移）；未应用动作核验原文件清单与
            # 裁决字段前态；新 blocker/计划外/失效动作在任何写入前阻断
            # （no-go 时数据与回执零变化）；最后 FOR UPDATE 锁定目标/预约
            # 行再应用，回执（含 repair 新预约）与动作原子提交。
            applied, skipped = [], []
            with pg_store.transaction(conn):
                with conn.cursor() as cur:
                    try:
                        already = _applied_action_keys(cur, plan)
                        _verify_successors(cur, upload_dir, plan, already)
                        _verify_file_evidence(upload_dir, plan, already)
                        state = collect(cur, upload_dir)
                        actions_now, blockers_now = plan_actions(
                            state, bool(plan["repair_residuals"]))
                        plan_keys = {a["action_key"] for a in plan["actions"]}
                        now_keys = {a["action_key"] for a in actions_now}
                        if blockers_now:
                            raise EvidenceError(
                                "环境漂移（新 blocker，写入前阻断）：%s"
                                % json.dumps(blockers_now,
                                             ensure_ascii=False)[:2000])
                        new_keys = now_keys - plan_keys - already
                        if new_keys:
                            raise EvidenceError("计划外动作（环境漂移/停写被"
                                                "破坏）：%s" % sorted(new_keys))
                        missing_keys = plan_keys - now_keys - already
                        if missing_keys:
                            raise EvidenceError("计划动作失效（前态漂移）："
                                                "%s" % sorted(missing_keys))
                        _verify_prestate(cur, plan, already, for_update=True)
                    except EvidenceError as exc:
                        print("预检 no-go：%s" % exc, file=sys.stderr)
                        return 3
                    for act in plan["actions"]:
                        cur.execute(
                            "INSERT INTO upload_capacity_repair_receipts "
                            "(action_key, plan_hash, target_kind, target_id,"
                            " action) VALUES (%s,%s,%s,%s,%s) ON CONFLICT"
                            " (action_key) DO NOTHING RETURNING action_key",
                            (act["action_key"], plan["plan_hash"],
                             act["kind"], act["id"], act["action"]))
                        if cur.fetchone() is None:
                            skipped.append(act["action_key"])
                            continue
                        result = apply_action(cur, act)
                        new_rid = result.get("repaired") or \
                            result.get("bound")
                        if new_rid:
                            cur.execute(
                                "UPDATE upload_capacity_repair_receipts SET"
                                " result_reservation_id=%s WHERE"
                                " action_key=%s",
                                (new_rid, act["action_key"]))
                        applied.append({"action_key": act["action_key"],
                                        "result": result})
            # 应用后重新 collect（输出应用后状态，不用应用前快照）
            with pg_store.transaction(conn):
                with conn.cursor() as cur:
                    state = collect(cur, upload_dir)
            actions_left, blockers_left = plan_actions(
                state, bool(plan["repair_residuals"]))
            report = {"mode": "apply", "plan_hash": plan["plan_hash"],
                      "applied": applied, "skipped": skipped,
                      "over_quota": state["over_quota"],
                      "pending_work": _report_pending_work(state),
                      "unexplained": _unexplained_after(
                          state, actions_left, blockers_left),
                      "note": ("超额已如实补记；新准入仍被 guard 拒绝"
                               if state["over_quota"] else None)}
            out = json.dumps(report, ensure_ascii=False, indent=2,
                             default=str)
            print(out)
            if args.json_out:
                with open(args.json_out, "w", encoding="utf-8") as fh:
                    fh.write(out)
            return 3 if report["unexplained"] else 0

        # dry-run / 冻结计划生成（证据错误=干净退出 3，不 traceback）
        try:
            with pg_store.transaction(conn):
                with conn.cursor() as cur:
                    state = collect(cur, upload_dir)
                    actions, blockers = plan_actions(state,
                                                     args.repair_residuals)
                    prestate = _prestate_for(cur, actions)
        except EvidenceError as exc:
            print("扫描 no-go（证据不完整）：%s" % exc, file=sys.stderr)
            return 3
        report = {"mode": "plan-out" if args.plan_out else "dry-run",
                  "tool_version": TOOL_VERSION,
                  "actions": _report_actions(actions), "blockers": blockers,
                  "over_quota": state["over_quota"],
                  "pending_work": _report_pending_work(state),
                  "note": "dry-run 仅预审；应用需冻结计划（--plan-out）"
                          " + --apply --plan"}
        if args.plan_out:
            plan = {"schema_version": SCHEMA_VERSION,
                    "tool_version": TOOL_VERSION,
                    "created_at": _dt.datetime.now(
                        _dt.timezone.utc).isoformat(),
                    "upload_root": upload_dir,
                    "repair_residuals": bool(args.repair_residuals),
                    "db_prestate": prestate,
                    "actions": actions}
            plan["plan_hash"] = _plan_hash(plan)
            with open(args.plan_out, "w", encoding="utf-8") as fh:
                json.dump(plan, fh, ensure_ascii=False, indent=2)
            report["plan_out"] = args.plan_out
            report["plan_hash"] = plan["plan_hash"]
        out = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        print(out)
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                fh.write(out)
        if blockers:
            return 3
        if _residual_without_duty(state, actions):
            return 3
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
