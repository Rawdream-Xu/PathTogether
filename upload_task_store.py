# -*- coding: utf-8 -*-
"""Upload V2 分片续传任务存储（docs/upload-resumable-fix-plan.md §3.1，U2）。

PostgreSQL 唯一后端：``upload_tasks`` 表（migrations/0017）。状态转移在
**短事务**内 ``SELECT ... FOR UPDATE`` 锁任务行（§3.1 跨 worker 锁）；整文件
哈希 / OpenSlide 验证 / 文件提升**不在行锁内**（§3.2.5 三段式，由 app.py
编排，本模块只提供状态原语）。json/dual 文件后端已删除。

状态机（§3.1，严格串行 offset）：

    active   -- PUT chunk offset==confirmed_offset --> active（confirmed_offset 前进）
    active   -- POST commit 受理 --> committing（短事务：commit_token + 续租）
    committing -- 收尾成功 --> committed
    committing -- 临时基础设施故障 --> active（可重试 commit）
    committing -- 确定性失败（哈希不匹配/非法切片） --> failed
    active   -- DELETE / TTL --> cancelled / expired
    failed   -- DELETE --> cancelled

公开 API（backend 无关，返回普通 dict；时间戳一律 epoch 秒 float）：
  create_task / get_task / append_chunk / begin_commit / finish_commit /
  fail_commit / rollback_committing / cancel_task / expire_task /
  begin_legacy_commit（V1 单请求直入 committing）/ list_tasks（恢复扫描与
  admin 观测的只读列举）

P3（slide ID 化重构，docs/slide-id-refactor-p3-contract-20260925.md）：
  - 任务行新增 slide_id（0067；allocate_slide 的唯一绑定源）与
    commit_intent_json（0068；publish intent 与置 committing 同事务写入、
    收口同事务清空——崩溃恢复按 intent 幂等重跑，见 slide_publish）。
  - begin_commit(intent=…) / begin_legacy_commit(slide_id=…, intent=…) 在
    CAS 内持久化 intent；generation 语义见 slide_publish（V2=commit_token，
    V1 单请求="1"）。
  - fail_active：新管线验证前置于受理后的确定性失败原语（active→failed）。
  - staging 布局（.staging/<task_id>/<generation>/）由 app/slide_storage
    派生，本模块不触文件系统。

错误类型（路由映射稳定错误码）：
  TaskNotFound / StateConflict / OffsetMismatch / ChunkConflict / SizeMismatch
  （后四者携带 .task 快照，供 409 响应回当前 confirmed_offset 等进度字段）。
"""

import json
import os
import secrets
import time

import pg_store

# --------------------------------------------------------------------------- #
# env 可调常量（import 期一次性读取；测试用 monkeypatch 改模块属性）
# --------------------------------------------------------------------------- #
def _env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


#: 任务 TTL（§3.2.4）：默认 24h；每次成功 PUT chunk 刷新 expires_at 并对
#: reservation 续租（upload_guard.renew_reservation）。
UPLOAD_TASK_TTL_SECONDS = _env_int("UPLOAD_TASK_TTL", 24 * 3600)

#: 服务端选定的分片大小（POST /api/uploads 返回给客户端）。
UPLOAD_CHUNK_SIZE = _env_int("UPLOAD_CHUNK_SIZE", 16 * 1024 * 1024)

#: 单片接收上限（防恶意客户端把一个 PUT 当无限大请求用；计数流截停）。
UPLOAD_CHUNK_MAX_BYTES = _env_int("UPLOAD_CHUNK_MAX_BYTES", 64 * 1024 * 1024)

#: commit 受理超时（§3.2.5 崩溃恢复）：committing 停留超过该秒数后惰性恢复。
UPLOAD_COMMIT_TIMEOUT_SECONDS = _env_int("UPLOAD_COMMIT_TIMEOUT", 600)


# --------------------------------------------------------------------------- #
# 状态常量与业务异常
# --------------------------------------------------------------------------- #
STATE_ACTIVE = "active"
STATE_COMMITTING = "committing"
STATE_COMMITTED = "committed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_EXPIRED = "expired"

ALL_STATES = (STATE_ACTIVE, STATE_COMMITTING, STATE_COMMITTED, STATE_FAILED,
              STATE_CANCELLED, STATE_EXPIRED)


class UploadTaskError(Exception):
    """上传任务存储业务异常基类。"""

    code = "upload_task_error"


class TaskNotFound(UploadTaskError):
    """任务不存在。"""

    code = "upload_not_found"


class StateConflict(UploadTaskError):
    """状态机不允许该转移（携带 .task 快照供响应回进度）。"""

    code = "upload_state_conflict"

    def __init__(self, message, task=None):
        super().__init__(message)
        self.task = task


class OffsetMismatch(UploadTaskError):
    """offset 与服务端确认点不对齐（串行模型只接受 ==confirmed_offset）。

    携带 .task 快照：409 响应回当前 confirmed_offset 供客户端对齐（§3.2.2）。
    """

    code = "offset_mismatch"

    def __init__(self, message, task=None):
        super().__init__(message)
        self.task = task


class ChunkConflict(UploadTaskError):
    """最后分片重放但 (length, sha256) 不一致（§3.2.1 幂等键冲突）。"""

    code = "chunk_conflict"

    def __init__(self, message, task=None):
        super().__init__(message)
        self.task = task


class SizeMismatch(UploadTaskError):
    """confirmed_offset != declared_size（commit 前置）或单片越过 declared_size。"""

    code = "size_mismatch"

    def __init__(self, message, task=None):
        super().__init__(message)
        self.task = task


# --------------------------------------------------------------------------- #
# 纯决策函数（双后端共享的状态机判定；输入 task dict）
# --------------------------------------------------------------------------- #
def _or0(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1  # 无 last_chunk_* 时不与任何合法 offset 相等


def _decide_append(task, offset, length, sha256):
    """PUT chunk 的串行 offset / 幂等判定（§3.2.1/§3.2.2）。

    返回 (action, fields)：
      - ``advanced``：offset == confirmed_offset 的新片，fields 为推进字段
        （confirmed_offset / last_chunk_*；expires_at 由调用方补）；
      - ``idempotent``：最后分片同键重放，不重复写（fields=None）；
      - ``progressed``：更早分片（非最后一片），直接回当前进度（fields=None）。
    抛 OffsetMismatch / ChunkConflict / StateConflict / SizeMismatch。
    """
    if task["state"] != STATE_ACTIVE:
        raise StateConflict("任务状态 %r 不可写入分片" % task["state"], task)
    confirmed = int(task["confirmed_offset"])
    if offset > confirmed:
        raise OffsetMismatch(
            "offset=%d 超前于服务端确认点 confirmed_offset=%d（严格串行）"
            % (offset, confirmed), task)
    if offset == confirmed:
        if offset + int(length) > int(task["declared_size"]):
            raise SizeMismatch(
                "分片写越界（offset+length=%d > declared_size=%d）"
                % (offset + int(length), int(task["declared_size"])), task)
        return "advanced", {
            "confirmed_offset": offset + int(length),
            "last_chunk_offset": offset,
            "last_chunk_length": int(length),
            "last_chunk_sha256": sha256,
        }
    # offset < confirmed_offset：幂等 / 冲突 / 更早分片三分支（§3.2.1）
    last_off = _or0(task.get("last_chunk_offset"))
    if offset == last_off:
        same = (int(length) == _or0(task.get("last_chunk_length"))
                and sha256 == (task.get("last_chunk_sha256") or ""))
        if same:
            return "idempotent", None
        raise ChunkConflict(
            "与最后已确认分片（offset=%d）的 length/sha256 不一致，拒绝幂等重放"
            % offset, task)
    if offset < last_off:
        # 更早的分片：直接返回当前进度，不声称完成哈希比对（§3.2.1）
        return "progressed", None
    # last_off < offset < confirmed：重叠分片（客户端中途改分片大小）→ 对齐重传
    raise OffsetMismatch(
        "offset=%d 与最后分片边界不对齐（confirmed_offset=%d，请从确认点续传）"
        % (offset, confirmed), task)


# --------------------------------------------------------------------------- #
# V1 artifact manifest（review-2026-08-29 §10.4 G7）
# --------------------------------------------------------------------------- #
def _encode_artifacts(artifacts):
    """校验并归一 V1 artifact manifest（[{name, size, sha256, slide}]）。

    返回新构造的 list[dict]（不引用调用方对象）；非法抛 UploadTaskError。
    name 允许嵌套 "/"（MRXS 伴侣目录），拒绝绝对路径 / ".." / 反斜杠 / 空名。
    """
    if not isinstance(artifacts, (list, tuple)) or not artifacts:
        raise UploadTaskError("artifact manifest 不能为空")
    out = []
    for a in artifacts:
        if not isinstance(a, dict):
            raise UploadTaskError("artifact 项需为 dict")
        name = a.get("name")
        if (not isinstance(name, str) or not name
                or name.startswith("/") or "\\" in name or "\x00" in name
                or any(p == ".." for p in name.split("/"))):
            raise UploadTaskError("artifact 名非法：%r" % (name,))
        try:
            size = int(a.get("size"))
        except (TypeError, ValueError):
            raise UploadTaskError("artifact size 需为整数：%r" % (a.get("size"),))
        if size < 0:
            raise UploadTaskError("artifact size 需为非负整数")
        sha = a.get("sha256")
        out.append({
            "name": name,
            "size": size,
            "sha256": (str(sha) if sha else None),
            "slide": bool(a.get("slide")),
        })
    if sum(o["size"] for o in out) <= 0:
        raise UploadTaskError("artifact 总字节数需为正")
    return out


def _artifacts_to_db(v):
    """list[dict] → PG TEXT 列的 JSON 字符串（None 原样）。"""
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _artifacts_from_db(v):
    """PG TEXT 列 → list（V2 任务为 None）。

    损坏（非法 JSON / 非数组）返回 **[]**（空列表）：与 None（=V2 任务）区分，
    调用方按「manifest 丢失但任务是 V1」的证据冲突 fail-closed，绝不猜。
    """
    if v is None:
        return None
    if isinstance(v, list):
        return v
    try:
        data = json.loads(v)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


# --------------------------------------------------------------------------- #
# 字段表（两后端同序；PG INSERT 显式列序依赖它）
# --------------------------------------------------------------------------- #
_TASK_FIELDS = (
    "upload_id", "owner_user_id", "filename", "safe_name", "declared_size",
    "chunk_size", "confirmed_offset", "last_chunk_offset", "last_chunk_length",
    "last_chunk_sha256", "sha256_expected", "sha256_actual", "reservation_id",
    "state", "commit_token", "commit_started_at", "expires_at", "created_at",
    "updated_at", "v1_artifacts",
    # 0067/0068（P3 合同 §2/§3.1）：单切片任务的资产绑定 + publish intent。
    # slide_id 是任务→资产的唯一绑定源（幂等重试复用原任务及其 ID）；
    # commit_intent_json 与任务置 committing 同事务写入（begin_commit /
    # begin_legacy_commit 的 CAS 内），发布收口短事务内清空。
    "slide_id", "commit_intent_json",
    # 0074（R15 核账合同）：创建时配额身份快照——duty/exempt；NULL=存量。
    "quota_mode",
)

# epoch 秒（float/int）入参 → timestamptz 的键
_TS_KEYS = ("commit_started_at", "expires_at", "created_at", "updated_at")


def _new_task_id():
    return "upt_" + secrets.token_hex(12)


def new_task_id():
    """预生成任务 ID（P3：V1 单文件在写盘期即建 .staging/<task_id>/ 暂存目录，
    任务行稍后由 begin_legacy_commit 以同一 ID 落库）。"""
    return _new_task_id()


def encode_commit_intent(payload):
    """publish intent dict → JSON 文本（None 原样；字段形态见 slide_publish）。"""
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def decode_commit_intent(raw):
    """commit_intent_json TEXT → dict；None（V2 无 intent/已收口）返回 None。

    损坏（非法 JSON/非对象）返回 ``{}``（空 dict，与 None 区分）——调用方按
    证据冲突 fail-closed（保持 committing 告警），绝不猜。
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _epoch_to_dt(v):
    import datetime
    return datetime.datetime.fromtimestamp(float(v), tz=datetime.timezone.utc)


# --------------------------------------------------------------------------- #
# PG 后端（upload_tasks 表；FOR UPDATE 短事务）
# --------------------------------------------------------------------------- #
_PG_COLS = ", ".join(_TASK_FIELDS)


def _pg_connect():
    import psycopg
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _norm_row(row):
    """PG 行 → 归一化 task dict（timestamptz → epoch 秒 float；数值 int 化）。"""
    if row is None:
        return None
    out = dict(row)
    for k in ("declared_size", "chunk_size", "confirmed_offset",
              "last_chunk_offset", "last_chunk_length"):
        if out.get(k) is not None:
            out[k] = int(out[k])
    for k in _TS_KEYS:
        v = out.get(k)
        out[k] = v.timestamp() if hasattr(v, "timestamp") else (float(v) if v else None)
    out["v1_artifacts"] = _artifacts_from_db(out.get("v1_artifacts"))
    return out


def _to_db_value(key, value):
    """task 字段 → PG 参数（epoch → timestamptz；manifest list → JSON 文本）。"""
    if key in _TS_KEYS and isinstance(value, (int, float)):
        return _epoch_to_dt(value)
    if key == "v1_artifacts":
        return _artifacts_to_db(value)
    return value


def _pg_update(conn, upload_id, fields):
    """短事务内（调用方已持 FOR UPDATE）按 fields UPDATE 并重选行。"""
    sets, params = [], []
    for k, v in fields.items():
        sets.append("%s = %%s" % k)
        params.append(_to_db_value(k, v))
    sets.append("updated_at = now()")
    params.append(upload_id)
    with conn.cursor() as cur:
        cur.execute("UPDATE upload_tasks SET %s WHERE upload_id = %%s"
                    % ", ".join(sets), tuple(params))
        cur.execute("SELECT %s FROM upload_tasks WHERE upload_id = %%s" % _PG_COLS,
                    (upload_id,))
        return cur.fetchone()


def _pg_apply(upload_id, mutate):
    """FOR UPDATE 短事务：锁行 → mutate(row)→(fields, result) → UPDATE。

    返回 (row_after_norm, result)；任务不存在返回 (None, None)。
    事务边界即锁边界：mutate/UPDATE 内不做任何重 IO（§3.2.5）。
    """
    conn = _pg_connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT %s FROM upload_tasks WHERE upload_id = %%s "
                            "FOR UPDATE" % _PG_COLS, (upload_id,))
                row = cur.fetchone()
            if row is None:
                return None, None
            task = _norm_row(row)
            fields, result = mutate(task)
            new_row = _pg_update(conn, upload_id, fields) if fields else row
            return _norm_row(new_row), result
    finally:
        conn.close()


def _apply(upload_id, mutate):
    """PG-only：FOR UPDATE 短事务内「锁内读-判-写」入口（见 _pg_apply 契约）。"""
    return _pg_apply(upload_id, mutate)


# --------------------------------------------------------------------------- #
# 公共 API（双后端统一）
# --------------------------------------------------------------------------- #
def _task_row(task):
    """task dict → 与 _TASK_FIELDS 同序的 PG INSERT 参数元组。"""
    return tuple(_to_db_value(k, task[k]) for k in _TASK_FIELDS)


def _quota_mode_snapshot(conn, owner_user_id):
    """创建时配额身份快照（0074；R15 核账合同）。

    与 upload_guard.quota_applies 同一身份语义：本地免登录（空 owner）→
    'exempt'（合法无预约）；role=owner|user → 'duty'（2026-11-06 owner
    parity：管理员登录账号与普通用户同口径）。无用户行时按 'duty' 落库
    （宁可多记责任），核账侧对不可证明身份另行阻断。"""
    import upload_guard

    uid = str(owner_user_id or "")
    if not uid:
        return "exempt"
    with conn.cursor() as cur:
        cur.execute("SELECT role FROM users WHERE user_id=%s", (uid,))
        row = cur.fetchone()
    if row is None:
        return "duty"
    return "duty" if (row["role"] or "") in upload_guard.QUOTA_ROLES \
        else "exempt"


def _pg_insert(task, conn=None):
    """INSERT 任务行；conn 给出时复用调用方事务（P3：allocate_slide 同事务绑定）。"""
    own = conn is None
    if own:
        conn = _pg_connect()
    try:
        if task.get("quota_mode") is None:
            task["quota_mode"] = _quota_mode_snapshot(
                conn, task.get("owner_user_id"))
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO upload_tasks (%s) VALUES (%s)"
                    % (_PG_COLS, ", ".join(["%s"] * len(_TASK_FIELDS))),
                    _task_row(task))
    finally:
        if own:
            conn.close()


def create_task(owner_user_id, filename, safe_name, declared_size, chunk_size,
                sha256_expected=None, reservation_id=None, ttl_seconds=None,
                slide_id=None, conn=None):
    """创建任务（state=active，confirmed_offset=0）。返回新任务 dict。

    P3（合同 §3.1.2）：``slide_id`` 是任务与预分配资产（slide_store
    .allocate_slide，storage_layout='id_bundle'）的唯一绑定——创建任务与绑定
    **同一事务**由调用方保证（app 侧 allocate_slide(conn=c) + create_task 经
    同一连接；本函数自身只写任务行）。幂等重试复用原任务及其 slide_id
    （V2 续传按 upload_id，不重新分配 ID）。
    """
    ttl = UPLOAD_TASK_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    now = time.time()
    task = {
        "upload_id": _new_task_id(),
        "owner_user_id": str(owner_user_id or ""),
        "filename": str(filename),
        "safe_name": str(safe_name),
        "declared_size": int(declared_size),
        "chunk_size": int(chunk_size),
        "confirmed_offset": 0,
        "last_chunk_offset": None,
        "last_chunk_length": None,
        "last_chunk_sha256": None,
        "sha256_expected": (str(sha256_expected) if sha256_expected else None),
        "sha256_actual": None,
        "reservation_id": (str(reservation_id) if reservation_id else None),
        "state": STATE_ACTIVE,
        "commit_token": None,
        "commit_started_at": None,
        "expires_at": now + ttl,
        "created_at": now,
        "updated_at": now,
        "v1_artifacts": None,
        "slide_id": (str(slide_id) if slide_id else None),
        "commit_intent_json": None,
        "quota_mode": None,
    }
    _pg_insert(task, conn=conn)
    return task


def begin_legacy_commit(owner_user_id, filename, safe_name, artifacts,
                        reservation_id=None, ttl_seconds=None,
                        slide_id=None, intent=None, upload_id=None, conn=None):
    """V1（旧单请求 /api/upload）的 commit 受理（review-2026-08-29 §10.4 G7）。

    与 V2「create_task → 分片 → begin_commit」不同：V1 无分片阶段，请求字节
    在受理前已全部落盘并通过内容验证，故「建任务 + 受理 commit（committing +
    commit_token + artifact manifest + reservation 绑定）」合并为**一次原子
    写**（PG 单条 INSERT 短事务）——崩溃只见完整旧/新版，不会留下无人认领的
    active 任务，也无需第二张补偿表。

    artifacts 为**提升之前**持久化的 manifest（_encode_artifacts 校验归一）；
    declared_size = confirmed_offset = settle_bytes = Σsize（提升后转实占的
    权威字节数）。返回 (upload_id, commit_token, task)。

    P3（合同 §3.1/§3.3）：
      - ``slide_id``：V1 原生单文件新管线的资产绑定——与任务行**同一事务**
        （调用方先 slide_store.allocate_slide(conn=conn)，本 INSERT 落在同一
        事务；conn 参数即为此服务）。
      - ``intent``：publish intent dict（slide_publish 六步第 3 步）——与置
        committing 同事务落 commit_intent_json；generation 固定 ``"1"``
        （V1 单请求无重试代次，合同 §3.2）。
      - ``upload_id``：调用方预生成的任务 ID（V1 单文件在写盘期即以该 ID 建
        ``.staging/<task_id>/1/`` 暂存目录）。
    """
    arts = _encode_artifacts(artifacts)
    total = sum(int(a["size"]) for a in arts)
    ttl = (UPLOAD_TASK_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds))
    # 任务 TTL 至少盖过 commit 超时窗口（同 begin_commit）
    ttl = max(ttl, 2 * UPLOAD_COMMIT_TIMEOUT_SECONDS)
    now = time.time()
    task = {
        "upload_id": upload_id or _new_task_id(),
        "owner_user_id": str(owner_user_id or ""),
        "filename": str(filename),
        "safe_name": str(safe_name),
        "declared_size": total,
        "chunk_size": 0,
        "confirmed_offset": total,
        "last_chunk_offset": None,
        "last_chunk_length": None,
        "last_chunk_sha256": None,
        "sha256_expected": None,
        "sha256_actual": None,
        "reservation_id": (str(reservation_id) if reservation_id else None),
        "state": STATE_COMMITTING,
        "commit_token": "uct_" + secrets.token_hex(16),
        "commit_started_at": now,
        "expires_at": now + ttl,
        "created_at": now,
        "updated_at": now,
        "v1_artifacts": arts,
        "slide_id": (str(slide_id) if slide_id else None),
        "commit_intent_json": None,
        "quota_mode": None,
    }
    if slide_id and intent is not None:
        payload = dict(intent)
        payload["task_ref"] = task["upload_id"]
        payload["generation"] = "1"
        payload["commit_token"] = task["commit_token"]
        task["commit_intent_json"] = encode_commit_intent(payload)
    _pg_insert(task, conn=conn)
    return task["upload_id"], task["commit_token"], task


def list_tasks(*, owner_user_id=None, state=None):
    """任务快照列举（不加锁读；恢复扫描与 admin 观测用，§10.4 G7）。

    过滤条件相与；owner_user_id="" 匹配本地免登录 owner 归一后的任务。
    返回 list[task]（created_at 升序 + upload_id tie-break）。
    """
    sql = "SELECT %s FROM upload_tasks" % _PG_COLS
    conds, params = [], []
    if owner_user_id is not None:
        conds.append("owner_user_id = %s")
        params.append(str(owner_user_id))
    if state is not None:
        conds.append("state = %s")
        params.append(str(state))
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY created_at, upload_id"
    conn = _pg_connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                return [_norm_row(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_task(upload_id):
    """读任务（不加锁的快照读）。不存在返回 None。"""
    conn = _pg_connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT %s FROM upload_tasks WHERE upload_id = %%s"
                            % _PG_COLS, (upload_id,))
                return _norm_row(cur.fetchone())
    finally:
        conn.close()


def append_chunk(upload_id, offset, length, sha256, *, ttl_seconds=None):
    """PUT chunk 的状态转移（短事务/文件锁内判定 + 推进）。

    分片字节的 pwrite 由调用方在**本调用之前**完成，但必须与本调用处于同一
    每任务写租约内（app.py ``_upload_v2_chunk_lock``），避免同 offset 并发
    覆盖。本调用只做对齐判定与 confirmed_offset 推进。

    返回 (action, task)：action ∈ advanced / idempotent / progressed。
    advanced 与 idempotent 刷新任务 expires_at（§3.2.4；progressed 只读回进度）。
    """
    offset, length = int(offset), int(length)
    ttl = UPLOAD_TASK_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)

    def _mutate(task):
        action, fields = _decide_append(task, offset, length, sha256)
        if action == "advanced":
            fields = dict(fields)
            fields["expires_at"] = time.time() + ttl
        return fields, action

    task, action = _apply(upload_id, _mutate)
    if task is None:
        raise TaskNotFound("上传任务不存在：%r" % upload_id)
    return action, task


def begin_commit(upload_id, *, ttl_seconds=None, intent=None):
    """commit 三段式的短事务 A：active → committing，写 commit_token。§3.2.5

    前置（锁内权威）：state=active 且 confirmed_offset == declared_size。
    返回 (commit_token, task)。

    P3（合同 §3.3 第 3 步）：``intent`` 给出时（V2 原生单文件新管线），publish
    intent 与任务置 committing **同一事务**落 ``commit_intent_json``——token
    由本 CAS 生成，注入 intent 的 ``generation``/``commit_token`` 字段后存储
    （generation = commit_token：崩溃恢复可重判的任务代次，合同 §3.2）。
    """
    ttl = (UPLOAD_TASK_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds))
    # 任务 TTL 至少盖过 commit 超时窗口，避免受理后任务先于恢复判定过期
    ttl = max(ttl, 2 * UPLOAD_COMMIT_TIMEOUT_SECONDS)

    def _mutate(task):
        if task["state"] != STATE_ACTIVE:
            raise StateConflict(
                "任务状态 %r 不可受理 commit（仅 active）" % task["state"], task)
        if int(task["confirmed_offset"]) != int(task["declared_size"]):
            raise SizeMismatch(
                "confirmed_offset=%d 未达 declared_size=%d，未传完不可 commit"
                % (int(task["confirmed_offset"]), int(task["declared_size"])), task)
        now = time.time()
        token = "uct_" + secrets.token_hex(16)
        fields = {"state": STATE_COMMITTING, "commit_token": token,
                  "commit_started_at": now, "expires_at": now + ttl}
        if intent is not None:
            payload = dict(intent)
            payload["task_ref"] = upload_id
            payload["generation"] = token
            payload["commit_token"] = token
            fields["commit_intent_json"] = encode_commit_intent(payload)
        return fields, token

    task, token = _apply(upload_id, _mutate)
    if task is None:
        raise TaskNotFound("上传任务不存在：%r" % upload_id)
    return token, task


def finish_commit(upload_id, commit_token, sha256_actual, *, settle_bytes=None):
    """commit 三段式的短事务 B：token 匹配且仍 committing → committed。§3.2.5

    token 不匹配 / 状态已变 → StateConflict（进程外崩溃后的惰性恢复凭同一
    token 收口；过时 worker 的收口被拒）。

    任务绑定 reservation 时，``settle_bytes`` 把配额转实占放进**同一事务**
    （任务 committed 与 used_bytes 同时提交 / 同时回滚）。预占失效抛
    ``upload_guard.ReservationInvalid``，任务行保持 committing。
    """

    return _pg_finish_commit(upload_id, commit_token, sha256_actual,
                             settle_bytes=settle_bytes)


def _pg_finish_commit(upload_id, commit_token, sha256_actual, *, settle_bytes=None):
    """PG：锁任务行 →（可选）同事务 consume reservation → committed。"""
    import upload_guard
    conn = _pg_connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT %s FROM upload_tasks WHERE upload_id = %%s "
                            "FOR UPDATE" % _PG_COLS, (upload_id,))
                row = cur.fetchone()
            if row is None:
                raise TaskNotFound("上传任务不存在：%r" % upload_id)
            task = _norm_row(row)
            if (task["state"] != STATE_COMMITTING
                    or task.get("commit_token") != commit_token):
                raise StateConflict(
                    "commit 收口失败：任务已不在受理态或 token 不匹配（state=%r）"
                    % task["state"], task)
            rid = task.get("reservation_id")
            if rid:
                if settle_bytes is None:
                    raise UploadTaskError(
                        "任务绑定 reservation 时 finish_commit 必须提供 settle_bytes")
                with conn.cursor() as cur:
                    upload_guard.consume_reservation_locked(
                        cur, rid, int(settle_bytes),
                        expect_holder=("upload_task", upload_id))
            fields = {"state": STATE_COMMITTED,
                      "sha256_actual": sha256_actual or None,
                      # P3（合同 §3.3 第 6 步）：收口同事务清空 publish intent
                      #（legacy 任务本就 NULL，no-op）。
                      "commit_intent_json": None}
            new_row = _pg_update(conn, upload_id, fields)
            return _norm_row(new_row)
    finally:
        conn.close()


def fail_commit(upload_id, commit_token, *, permanent, sha256_actual=None,
                ttl_seconds=None):
    """committing 的失败转移（§3.1 失败类型区分）。

    - ``permanent=True``（确定性失败：整文件哈希不匹配 / _validate_slide_file
      判非法）→ failed：原内容不可改，只能 DELETE 取消后重新上传；
    - ``permanent=False``（临时基础设施故障）→ active：清 token，可重试 commit。
    幂等保护：仅当 state=committing 且 commit_token 匹配时生效（过时 worker
    不得覆盖新一次 commit 受理）。
    """
    ttl = (UPLOAD_TASK_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds))

    def _mutate(task):
        if (task["state"] != STATE_COMMITTING
                or task.get("commit_token") != commit_token):
            raise StateConflict(
                "commit 失败转移被拒：任务已不在受理态或 token 不匹配（state=%r）"
                % task["state"], task)
        if permanent:
            fields = {"state": STATE_FAILED}
            if sha256_actual:
                fields["sha256_actual"] = sha256_actual
        else:
            now = time.time()
            fields = {"state": STATE_ACTIVE, "commit_token": None,
                      "commit_started_at": None, "expires_at": now + ttl}
        return fields, None

    task, _ = _apply(upload_id, _mutate)
    if task is None:
        raise TaskNotFound("上传任务不存在：%r" % upload_id)
    return task


def fail_active(upload_id, *, ttl_seconds=None, sha256_actual=None):
    """P3（合同 §3.3）：active 任务的确定性失败 → failed（无 token 版）。

    V2 新管线把内容验证（哈希/格式）放在 begin_commit **之前**，非法内容的
    确定性失败发生时任务尚在 active（无 commit_token，fail_commit 不可用）。
    分片已确认的内容不可修复重传，故直接 active → failed（调用方随后清
    staging、释放预占、staging 资产行 mark_failed）。其余状态 StateConflict。
    sha256_actual（可选）随失败落库（证据，同 fail_commit 口径）。
    """
    ttl = (UPLOAD_TASK_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds))

    def _mutate(task):
        if task["state"] != STATE_ACTIVE:
            raise StateConflict(
                "仅 active 可直接判失败（当前 %r）" % task["state"], task)
        fields = {"state": STATE_FAILED,
                  "expires_at": time.time() + ttl}
        if sha256_actual:
            fields["sha256_actual"] = str(sha256_actual)
        return fields, None

    task, _ = _apply(upload_id, _mutate)
    if task is None:
        raise TaskNotFound("上传任务不存在：%r" % upload_id)
    return task


def rollback_committing(upload_id, *, ttl_seconds=None):
    """惰性恢复原语：committing（已超时）→ active，清 commit 凭据。§3.2.5

    与 fail_commit(permanent=False) 的区别：不校验 token（恢复由当前访问者
    发起，旧 token 无意义），仅要求仍处 committing。
    """
    ttl = (UPLOAD_TASK_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds))

    def _mutate(task):
        if task["state"] != STATE_COMMITTING:
            raise StateConflict(
                "仅 committing 可回滚（当前 %r）" % task["state"], task)
        now = time.time()
        return {"state": STATE_ACTIVE, "commit_token": None,
                "commit_started_at": None, "expires_at": now + ttl}, None

    task, _ = _apply(upload_id, _mutate)
    if task is None:
        raise TaskNotFound("上传任务不存在：%r" % upload_id)
    return task


def cancel_task(upload_id):
    """DELETE 取消：active|failed → cancelled；cancelled|expired 幂等返回。

    committing / committed → StateConflict（路由映射 409：不阻塞等待长事务，
    也不允许撤回已完成的入库，§3.2.5）。
    """

    def _mutate(task):
        s = task["state"]
        if s in (STATE_ACTIVE, STATE_FAILED):
            return {"state": STATE_CANCELLED, "commit_token": None}, s
        if s in (STATE_CANCELLED, STATE_EXPIRED):
            return None, s  # 幂等：不写
        raise StateConflict("任务状态 %r 不可取消" % s, task)

    task, _ = _apply(upload_id, _mutate)
    if task is None:
        raise TaskNotFound("上传任务不存在：%r" % upload_id)
    return task


def expire_task(upload_id):
    """TTL 到期（惰性触发）：active → expired；其余状态原样返回（不写）。"""

    def _mutate(task):
        if task["state"] == STATE_ACTIVE:
            return {"state": STATE_EXPIRED}, None
        return None, None

    task, _ = _apply(upload_id, _mutate)
    if task is None:
        raise TaskNotFound("上传任务不存在：%r" % upload_id)
    return task


# --------------------------------------------------------------------------- #
# upload_task_items（0067 批量任务绑定；P4-app V1 ZIP 接线）
# --------------------------------------------------------------------------- #
def bind_upload_task_item(conn, task_id, item_key, slide_id):
    """绑定批量任务的逻辑切片 → 预分配 slide_id（调用方事务内）。

    幂等（R-13）：(task_id, item_key) 已有行 → 返回**既有行**的 slide_id
    （重试/恢复复用，绝不重新分配）；slide_id 全局 UNIQUE（一个资产只属
    一个任务项）兜底并发误绑。必须与 allocate_slide 同一事务（调用方保证）
    ——崩溃只见完整旧/新版。
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO upload_task_items (task_id, item_key, slide_id) "
            "VALUES (%s,%s,%s) "
            "ON CONFLICT (task_id, item_key) DO NOTHING "
            "RETURNING item_key, slide_id",
            (str(task_id), str(item_key), str(slide_id)))
        row = cur.fetchone()
        if row is not None:
            return {"item_key": row["item_key"], "slide_id": row["slide_id"]}
        cur.execute(
            "SELECT item_key, slide_id FROM upload_task_items "
            "WHERE task_id=%s AND item_key=%s", (str(task_id), str(item_key)))
        row = cur.fetchone()
        if row is None:
            raise UploadTaskError("upload_task_items 绑定失败：%r/%r"
                                  % (task_id, item_key))
        return {"item_key": row["item_key"], "slide_id": row["slide_id"]}


def list_upload_task_items(task_id):
    """批量任务的 item 绑定列表（item_key 升序；不加锁读）。无行返回 []。"""
    conn = _pg_connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT item_key, slide_id FROM upload_task_items "
                    "WHERE task_id=%s ORDER BY item_key", (str(task_id),))
                return [{"item_key": r["item_key"], "slide_id": r["slide_id"]}
                        for r in cur.fetchall()]
    finally:
        conn.close()


def get_upload_task_item(task_id, item_key):
    """单 item 绑定（无行返回 None）。"""
    conn = _pg_connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT item_key, slide_id FROM upload_task_items "
                    "WHERE task_id=%s AND item_key=%s",
                    (str(task_id), str(item_key)))
                r = cur.fetchone()
                return ({"item_key": r["item_key"], "slide_id": r["slide_id"]}
                        if r is not None else None)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 暂存清理失败的持久待清理状态（0071；R6 审查问题 3 修复）
# --------------------------------------------------------------------------- #
def record_cleanup_pending(upload_id, reservation_id=None, *, error=None):
    """清理失败 → 落/更新待清理行（可重试证据；**只登记，不动账本**）。

    生命周期（0072 / plan §4.2「清理失败保留重试工作和容量」）：取消/失败/
    过期路径**不再先释放后清理**——任务终态时预约保持绑定+reserved（容量
    责任不清零），本函数只登记清理重试工作；重试成功由清理确认路径
    （clear_cleanup_pending 的调用方）按持有者释放。R7–R9 时代的
    「released → reserved 重激活 + 配额补记 + 30 天延期」补账已随绑定模型
    拆除（任务持有的容量从不被 TTL 回收，不存在需要补回的窗口）。

    幂等（同 upload_id 更新 attempts+1，last_error 截断 4KB）。"""
    conn = _pg_connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                rid = (reservation_id or "").strip() or None
                cur.execute(
                    "INSERT INTO upload_cleanup_pending "
                    "(upload_id, reservation_id, attempts, last_error) "
                    "VALUES (%s, %s, 1, %s) "
                    "ON CONFLICT (upload_id) DO UPDATE SET "
                    "attempts = upload_cleanup_pending.attempts + 1, "
                    "reservation_id = COALESCE(EXCLUDED.reservation_id, "
                    "upload_cleanup_pending.reservation_id), "
                    "last_error = EXCLUDED.last_error, "
                    "updated_at = now()",
                    (str(upload_id), rid,
                     (str(error)[:4096] if error else None)))
    finally:
        conn.close()


def clear_cleanup_pending(upload_id):
    """清理确认成功 → 删待清理行。返回被删行的 reservation_id（供调用方
    在清理确认后释放预占），无行返回 None。

    R12 §3.3：**释放与删行必须同事务**——正式入口是
    ``confirm_cleanup_and_release``（task → quota → reservation 单事务，
    释放责任与删 pending 原子提交）。本函数只保留给只删行的观测路径。
    """
    conn = _pg_connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM upload_cleanup_pending WHERE upload_id=%s "
                    "RETURNING reservation_id", (str(upload_id),))
                row = cur.fetchone()
                # _pg_connect 是 dict_row——按列名取（R7 复核修复 P2：
                # row[0] 在有行时必抛 KeyError，事务回滚、pending 行残留）。
                return (row["reservation_id"] if row else None)
    finally:
        conn.close()


def confirm_cleanup_and_release(upload_id, *, fallback_reservation_id=None):
    """清理确认收口（R12 §3.3 单事务）：释放预约 + 删 pending 行同一事务。

    锁序：upload_tasks 行 → quota 行 → reservation 行（→ pending 行删除）。
    持有者语境（upload_task）：绑定预约只经清理确认释放。幂等：pending
    无行且预约已非 reserved → no-op；DB 失败整笔回滚（pending 保留，
    不出现「删了重试记录却没释放/反之」的半收口）。返回释放的
    reservation_id（未释放返回 None）。
    """
    import upload_guard
    conn = _pg_connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT reservation_id FROM upload_tasks "
                    "WHERE upload_id=%s FOR UPDATE", (str(upload_id),))
                task = cur.fetchone()
                cur.execute(
                    "SELECT reservation_id FROM upload_cleanup_pending "
                    "WHERE upload_id=%s FOR UPDATE", (str(upload_id),))
                pending = cur.fetchone()
                rid = ((pending["reservation_id"] if pending else None)
                       or fallback_reservation_id
                       or (task["reservation_id"] if task else None))
                if not rid:
                    return None
                out = upload_guard.release_reservation_locked(
                    cur, rid, expect_holder=("upload_task", str(upload_id)))
                if pending is not None:
                    cur.execute(
                        "DELETE FROM upload_cleanup_pending "
                        "WHERE upload_id=%s", (str(upload_id),))
                return rid if out.get("state") == "released" else None
    finally:
        conn.close()


def get_cleanup_pending(upload_id):
    """待清理行（None=无）。观测/重试路径用。"""
    conn = _pg_connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT upload_id, reservation_id, attempts, last_error "
                    "FROM upload_cleanup_pending WHERE upload_id=%s",
                    (str(upload_id),))
                row = cur.fetchone()
                return dict(row) if row is not None else None
    finally:
        conn.close()
