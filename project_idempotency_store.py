# -*- coding: utf-8 -*-
"""项目创建幂等存储层（W3：project-create dialog contract + Idempotency-Key）。

背景：新 UI 的创建对话框为每份草稿生成一个 ``Idempotency-Key``（编辑负载
即换新键），网络重试重放同键同负载必须返回**原项目**而非再建一个。旧客户端
不带键 → 维持现状（每次调用随机 pid 各建一个，不写幂等表）。

契约（migrations/0050_project_create_idempotency.sql）：
  - 唯一性 = ``(owner_user_id, idempotency_key)``；跨用户同键互不影响；
  - 同键同负载（canonical JSON sha256 相同）→ 返回原项目，不建第二个；
  - 同键不同负载 → :class:`IdempotencyConflict`（HTTP 409）；
  - 同名不同键**允许**建两个项目（不做按名去重）；
  - projects 行与幂等行**同一事务**写入（先插项目再插幂等记录，任一失败
    整体回滚），绝不出现「有项目无幂等结果」或反之。

并发：同 (owner, key) 以事务级 advisory lock 串行化
（``pg_advisory_xact_lock(hashtext('prj_idem:'||owner||':'||key))``），
锁内先查后插——两个并发同键同负载请求只有一个 pid 落地。

与 app.py 的分工（W3）：
  - 本模块只做**类型/形状**校验（:func:`validate_create_payload`，400 语义）
    与幂等写入；切片**归属/存在性**校验（app.py
    ``_resolve_slide_refs_for_project``，P0-W2 起）仍在 HTTP 层，HTTP 层把
    已解析校验的 slide 名传给本存储。
"""

import hashlib
import json
import secrets
import time

import psycopg

import pg_store
import share_store
import share_store_pg
from share_shared import _reject_guest_write
from share_store_pg import _PROJ_SEL, _dedupe

#: 项目名上限（前端同限；服务端 projects.name 为 TEXT 无更严约束，故以
#: 本常量为权威——前后端同 60）。
NAME_MAX_LEN = 60
#: 备注上限（前端同限；projects.note 为 TEXT 无更严约束）。
NOTE_MAX_LEN = 200
#: Idempotency-Key 上限（防滥用；请求头值理应远短于此）。
IDEMPOTENCY_KEY_MAX_LEN = 200


class IdempotencyConflict(Exception):
    """同 (owner, key) 已绑定**不同**负载（非重放）→ HTTP 409。"""

    code = "idempotency_key_conflict"


class PayloadInvalid(Exception):
    """创建负载类型/形状非法（name/note/slides 字段级违规）→ HTTP 400。"""


# --------------------------------------------------------------------------- #
# canonical 负载摘要
# --------------------------------------------------------------------------- #
def canonical_payload_digest(name, note, slides, parent_project_id=None):
    """返回 {name, note, slides, parent_project_id} canonical JSON 的 sha256。

    - 键序固定 name/note/slides/parent_project_id，``separators=(',', ':')``，
      ``ensure_ascii=False``；
    - slides **不排序**——顺序影响 project_slides.position，属负载语义；
      摘要针对清洗后的精确列表顺序；
    - 2026-10-08 §5.3：parent_project_id（None=根）属负载语义——同键换父
      级是不同负载（409，不静默改语义）。
    """
    payload = {"name": name, "note": note, "slides": list(slides or []),
               "parent_project_id": parent_project_id or None}
    canonical = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 负载校验（400 语义）
# --------------------------------------------------------------------------- #
def sanitize_slide_names(slides):
    """存储级 slide 名清洗：必须 list[str]；逐个 strip，拒绝空名/NUL/路径分隔符。

    与 app.py ``_sanitize_name`` 的越权部分对齐（NUL、``/``、``\\``、裸
    ``.``/``..``）；**不做**归属/存在性检查——那是 HTTP 层
    ``_resolve_slide_refs_for_project`` 的职责（本函数接受已过归属校验的名）。
    返回清洗后的新列表（保序）；非法即抛 :class:`PayloadInvalid`。
    """
    if slides is None:
        return []
    if not isinstance(slides, list):
        raise PayloadInvalid("slides 需为数组")
    clean = []
    for s in slides:
        if not isinstance(s, str):
            raise PayloadInvalid("slides 含非法文件名")
        name = s.strip()
        if not name:
            raise PayloadInvalid("slides 含空文件名")
        if "\x00" in name or "/" in name or "\\" in name:
            raise PayloadInvalid("非法文件名: " + s)
        if name == "." or name == "..":
            raise PayloadInvalid("非法文件名: " + s)
        clean.append(name)
    return clean


def validate_create_payload(body):
    """校验创建负载，返回 ``(name, note, slides)``（清洗后）。

    规则（与前端一致）：
      - body 必须 dict；
      - name 必须 str；strip 后非空且 ≤ NAME_MAX_LEN(60)；
      - note 缺省 ""；存在时必须 str 且 ≤ NOTE_MAX_LEN(200)；
      - slides 缺省 []；存在时必须 list[str]——dict/数字/None/字符串**不得**
        被静默吞成空列表，一律 :class:`PayloadInvalid`。

    非法即抛 :class:`PayloadInvalid`（HTTP 层映射 400）。
    """
    if not isinstance(body, dict):
        raise PayloadInvalid("请求体需为 JSON 对象")

    name = body.get("name")
    if not isinstance(name, str):
        raise PayloadInvalid("name 需为字符串")
    name = name.strip()
    if not name:
        raise PayloadInvalid("name 不能为空")
    if len(name) > NAME_MAX_LEN:
        raise PayloadInvalid("name 长度不得超过 %d 字符" % NAME_MAX_LEN)

    if "note" in body and body["note"] is not None:
        note = body["note"]
        if not isinstance(note, str):
            raise PayloadInvalid("note 需为字符串")
        if len(note) > NOTE_MAX_LEN:
            raise PayloadInvalid("note 长度不得超过 %d 字符" % NOTE_MAX_LEN)
    else:
        note = ""

    if "slides" not in body:
        slides = []
    else:
        raw = body["slides"]
        if not isinstance(raw, list):
            # 显式非 list（dict/str/数字/None）绝不静默吞成空列表
            raise PayloadInvalid("slides 需为数组")
        slides = sanitize_slide_names(raw)
    return name, note, slides


def _normalize_idempotency_key(idempotency_key):
    """规整请求键：None/空白 → None（走旧随机 pid 路径）；非法类型抛 400。"""
    if idempotency_key is None:
        return None
    if not isinstance(idempotency_key, str):
        raise PayloadInvalid("Idempotency-Key 需为字符串")
    key = idempotency_key.strip()
    if not key:
        return None
    if len(key) > IDEMPOTENCY_KEY_MAX_LEN:
        raise PayloadInvalid("Idempotency-Key 过长")
    return key


# --------------------------------------------------------------------------- #
# 幂等创建
# --------------------------------------------------------------------------- #
def _fetch_project_row(cur, pid):
    """按 create_project 同款 SELECT 取项目 dict（含 slides，epoch 时间）。"""
    cur.execute("SELECT " + _PROJ_SEL + " FROM projects WHERE project_id=%s", (pid,))
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute("SELECT slide FROM project_slides WHERE project_id=%s "
                "ORDER BY position", (pid,))
    d = dict(row)
    d["pid"] = pid
    d["slides"] = [r["slide"] for r in cur.fetchall()]
    return d


def _insert_idempotency_row(cur, owner_user_id, idempotency_key,
                            payload_sha256, project_id):
    """写入幂等行（独立函数便于原子性测试注入失败）。

    必须与项目行同事务调用——本函数失败时外层事务整体回滚。
    """
    cur.execute(
        "INSERT INTO project_create_idempotency "
        "(owner_user_id, idempotency_key, payload_sha256, project_id) "
        "VALUES (%s,%s,%s,%s)",
        (owner_user_id, idempotency_key, payload_sha256, project_id))


def create_project_idempotent(*, name, note="", slides=None, owner_user_id=None,
                              requester_role=None, idempotency_key=None,
                              parent_project_id=None):
    """创建项目（可选按 (owner, key) 幂等）。返回项目 dict（含 pid）。

    - ``idempotency_key`` 缺省/空白 → 直接走既有 ``share_store.create_project``
      （随机 pid，不写幂等行；同名同负载两次调用即两个项目）；
    - 2026-10-08 §5.3：``parent_project_id`` 透传建子文件夹；层级校验在
      share_store_pg 的事务内（ProjectParentError 原样上抛）。
    - 键存在 → 单事务内：advisory lock → 查幂等行 →
        - 命中且摘要一致：读回原项目返回（形状与首次创建相同，另附
          ``duplicate=True`` 便于调用方区分；HTTP 层可忽略该字段）；
        - 命中且摘要不同：抛 :class:`IdempotencyConflict`（409）；
        - 未命中：插 projects + project_slides（SQL 与
          share_store_pg.create_project 一致）再插幂等行，同事务提交。

    guest 写拒绝与 create_project 同源（``_reject_guest_write`` →
    PermissionError，HTTP 403）。
    """
    _reject_guest_write(requester_role)
    key = _normalize_idempotency_key(idempotency_key)
    if key is None:
        # 旧客户端路径：行为与现状完全一致（含默认名/去重逻辑）。
        return share_store.create_project(
            name=name, note=note, slides=slides,
            owner_user_id=owner_user_id, requester_role=requester_role,
            parent_project_id=parent_project_id)

    # 与 create_project 相同的形状规整（幂等负载以规整后为准）
    proj_name = str(name or "").strip() or "未命名项目"
    proj_note = str(note or "")
    uniq = _dedupe(slides)
    digest = canonical_payload_digest(proj_name, proj_note, uniq,
                                       parent_project_id)

    conn = pg_store.connect()
    try:
        conn.row_factory = psycopg.rows.dict_row
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # 同 (owner, key) 串行化：并发同键同负载只有一个事务插项目
                cur.execute(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext('prj_idem:' || %s || ':' || %s))",
                    (owner_user_id or "", key))
                cur.execute(
                    "SELECT payload_sha256, project_id "
                    "FROM project_create_idempotency "
                    "WHERE owner_user_id=%s AND idempotency_key=%s",
                    (owner_user_id or "", key))
                row = cur.fetchone()
                if row is not None:
                    if row["payload_sha256"] != digest:
                        raise IdempotencyConflict(
                            "Idempotency-Key 已绑定其他负载（非重放）")
                    out = _fetch_project_row(cur, row["project_id"])
                    if out is None:  # pragma: no cover - FK 保证不可达
                        raise IdempotencyConflict(
                            "幂等记录指向的项目不存在")
                    out["duplicate"] = True
                    return out

                # §5.3：父级校验（锁 owner 行 + 层级/同 owner/归档），
                # ProjectParentError 原样上抛（HTTP 层映射 400/403/409）
                if parent_project_id:
                    share_store_pg._validate_parent_tx(
                        cur, owner_user_id or None, parent_project_id)
                pid = "prj_" + secrets.token_urlsafe(10)
                now = time.time()
                cur.execute(
                    "INSERT INTO projects (project_id, name, note, "
                    "owner_user_id, created_at, parent_project_id) "
                    "VALUES (%s,%s,%s,%s, to_timestamp(%s), %s)",
                    (pid, proj_name, proj_note, owner_user_id or None, now,
                     parent_project_id or None))
                for i, s in enumerate(uniq):
                    cur.execute(
                        "INSERT INTO project_slides (project_id, slide, "
                        "position) VALUES (%s,%s,%s)", (pid, s, i))
                _insert_idempotency_row(cur, owner_user_id or "", key,
                                        digest, pid)
                return {
                    "pid": pid,
                    "name": proj_name,
                    "note": proj_note,
                    "slides": uniq,
                    "created_at": now,
                    "owner_user_id": owner_user_id or None,
                    "archived": False,
                    "parent_project_id": parent_project_id or None,
                }
    finally:
        conn.close()
