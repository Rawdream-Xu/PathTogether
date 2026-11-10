# -*- coding: utf-8 -*-
"""切片分享 —— 共享存储层 PostgreSQL 后端实现（Stage 3b-2）。

逐函数对照 JSON 实现语义移植（44 个公共名全实现 + 稳定 slide 身份；共有常量/纯
函数取自 IO-free 的 `share_shared`）。
调用方仍经 `share_store` dispatcher 访问（`STORAGE_BACKEND=postgres` 时 re-export
本模块），app.py / share_server.py / tests 一行不改。

数据模型（对应 migrations/0001_init.sql + 0002_roi_payload.sql）：
  - shares       → shares（slides/permissions/roi_sizes 暂保 JSONB 数组形态）
  - grants       → grants（active 布尔 ⇔ json 的 revoked_at is None）
  - rois         → rois（权威 dict 存 data JSONB，离散列镜像供过滤；insert_seq
                    保证 token 内插入序 = json 文件内数组顺序）
  - change_log   → change_log（bigserial seq 即全局单调序号；json 是 per-slide
                    计数器，两者数值不同——允许的实现差，见模块 docstring）
  - slide_meta   → slides（name=legacy_filename；稳定 slide_id 在此生成/维护）
  - projects     → projects / project_slides

与 JSON 实现的**允许实现差**（测试断言 seq 具体值需归类为 json-only）：
  - json 的 change_seq 是 per-slide 计数器；PG 用 change_log 的全局 bigserial seq。
    `list_changes(slide, after_seq)` / `current_change_seq(slide)` 在 PG 按全局 seq
    过滤/取值，语义（单调、按 slide 过滤）一致，但数值不同。
  - 时间戳在库中为 TIMESTAMPTZ，读出统一转 epoch 浮点，与 json 的浮点形状一致。

稳定 slide 身份（本节点文档验收点，见 docs §Stage 3b）：
  - `set_slide_meta(name, ...)`：name（legacy_filename）首次出现 → 生成稳定
    slide_id（sld_ + 12 位 urlsafe）插入 slides 行；同名已存在 → 仅更新
    alias/note/owner/public，slide_id 不动。
  - `get_slide_id(name)` / `resolve_slide_ref(name)`：name ⇄ slide_id 映射查询。
  - `record_slide_asset(slide_id, legacy_revision)`：记录切片内容资产 revision
    （content_sha256 由 Stage 3b-3 迁移工具填充，本节点先用 legacy_revision 占位）。
"""

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid

import psycopg

import annotation_access
import pg_store
from share_shared import (
    ADMIN_TOKEN,
    DEFAULT_PERMISSIONS,
    PERMISSION_VIEW,
    PERMISSION_ANNOTATE,
    PERMISSION_DOWNLOAD,
    ROI_TYPES,
    ALLOWED_ROI_SIZES,
    DEFAULT_ROI_SIZES,
    _clean_note,
    _clean_comment_body,
    _cap_claim_permissions,
    _grant_out,
    _grant_permissions_of,
    _hash_installation_secret,
    _installation_out,
    _is_active,
    _effective_rect_geometry,
    _norm_label,
    _normalize_permissions,
    _normalize_roi_sizes,
    _normalize_rect_policy,
    _rect_read_compat,
    _reject_guest_write,
    _roi_shared_compat,
    _share_permissions,
    _share_roi_sizes,
    _share_rect_policy,
    _status_of,
    _validate_geom,
)

#: 本模块日志（audit 写失败 best-effort 的节流 exception 日志走这里；消息只含
#: action/target 标识与异常堆栈，绝不落 detail 负载/密钥——同 billing_store 红线）
_LOG = logging.getLogger(__name__)


class RevisionConflict(Exception):
    """CAS 失败：expected_revision 与当前 revision 不符（与 json 同语义）。

    携带 ``current_revision``（int）。pg 后端独立定义一份，保证 postgres 模式下
    ``share_store.RevisionConflict`` 与本模块抛出的类一致（json 的 _check_cas 不能
    直接复用——它引用 json 的 RevisionConflict）。
    """

    def __init__(self, current_revision, message=None):
        self.current_revision = int(current_revision)
        super().__init__(
            message or "revision 冲突：标注已被他人修改，请刷新后重试")


class AnnotationLocked(Exception):
    """几何位置锁冲突（docs/viewer-interaction-skills-review-design-20261009
    §6.3）：``geometry_locked=true`` 的标注拒绝几何变更与删除。

    语义边界：这是**防误改位置锁**，不是权限锁——备注/元数据/审核/授权
    变更不受限；切片删除、账号删除等生命周期清理不经本异常（统一删除编排
    不走 delete_roi*）。在**同一事务、行锁内**抛出（与 RevisionConflict 同
    位置）；检查先于 CAS——「已锁且改几何」与「revision 不符」同时成立时
    返回 annotation_locked。携带 ``current``（当前 roi dict 快照，供 HTTP
    层组装 409 current 投影，须经 public_roi_view 过滤后回传）。
    """

    def __init__(self, current, message=None):
        self.current = current if isinstance(current, dict) else {}
        super().__init__(message or "标注位置已锁定，请先解锁后再修改几何或删除")


class ShareStoreCorrupt(Exception):
    """PG 后端不落 shares.json 文件；本类仅为 dispatcher 公共名对齐（json 后端
    的文件损坏语义在 PG 模式不可达）。数据级损坏由 SQL 约束/事务保证不发生。"""


class ShareStoreUnavailable(Exception):
    """PG 后端连接/查询失败（等价 json 后端 EACCES/EIO 的分流）。

    连接异常时上抛本类，share_server 映射 503 share_store_unavailable
    （fail-closed，不回空库）。
    """


# 文件路径占位：PG 后端不用文件（dispatcher 公共名校验需要这些名字存在）
SHARE_DATA_DIR = None
SHARE_FILE = None
# 兼容测试里 `SHARE_FILE.write_text(...)` 等文件调用：PG 后端这些测试会被标记跳过，
# 故这里保持 None 即可（见 tests/pg_compat.json_only）。

# 常量（re-export 自 share_shared，dispatcher 公共名需要）
ROI_TYPES = ROI_TYPES
ALLOWED_ROI_SIZES = ALLOWED_ROI_SIZES
DEFAULT_ROI_SIZES = DEFAULT_ROI_SIZES
ADMIN_TOKEN = ADMIN_TOKEN
PERMISSION_VIEW = PERMISSION_VIEW
PERMISSION_ANNOTATE = PERMISSION_ANNOTATE
PERMISSION_DOWNLOAD = PERMISSION_DOWNLOAD
DEFAULT_PERMISSIONS = DEFAULT_PERMISSIONS


def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def probe_readable():
    """启动只读探针（与 json 后端公共名对齐）：SELECT 1 验证存储可用。

    连接/查询失败 → ``ShareStoreUnavailable``（worker 不 ready）；成功 True。
    """
    try:
        conn = _connect()
    except Exception as e:  # noqa: BLE001 - psycopg 各类连接错误统一分流
        raise ShareStoreUnavailable("share 存储 PG 连接失败：%s" % e) from e
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as e:  # noqa: BLE001
        raise ShareStoreUnavailable("share 存储 PG 探针查询失败：%s" % e) from e
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return True


# 数据归属：由 app.py 启动时注入首个 owner 的 user_id（与 json 的 _OWNER_USER_ID
# 语义一致，作为新建 roi/project/slide_meta 的缺省 owner）。
_OWNER_USER_ID = ""


def set_owner_user_id(user_id: str) -> None:
    """注入当前 owner 的 user_id（供数据归属缺省值使用）。"""
    global _OWNER_USER_ID
    _OWNER_USER_ID = user_id or ""


def get_owner_user_id() -> str:
    """读取当前注入的 owner user_id（P3：无 UID 本地模式的上传资产 owner
    解析口径——与 set_slide_meta 归属缺省值同一来源；空串 = 未配置）。"""
    return _OWNER_USER_ID or ""


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
# roi 几何字段（存 data JSONB，镜像到 geom 列）。升级 C（§6.3）：rect 的
# w/h/geometry_version 一并承接（历史快照与 geom 投影同一白名单）。
_GEOM_KEYS = ("x", "y", "side_px", "size_mm", "w", "h", "geometry_version",
              "x1", "y1", "x2", "y2", "points")


def _geom_of(roi: dict) -> dict:
    return {k: roi[k] for k in _GEOM_KEYS if k in roi}


def _check_geometry_lock(roi, changing_geometry):
    """行锁内几何锁检查（§6.3）：已锁且本请求改几何（或删除）→ AnnotationLocked。

    在 ``_check_cas`` **之前**调用——annotation_locked 优先于 revision_conflict
    （前端按错误码区分「先解锁」与「版本冲突」两种提示）。
    """
    if changing_geometry and roi.get("geometry_locked"):
        raise AnnotationLocked(roi)


def _geometry_lock_read(roi: dict) -> dict:
    """读侧锁状态归一：旧行无 geometry_locked 键 → 补 false（就地，仅输出副本）。"""
    if isinstance(roi, dict) and not roi.get("deleted"):
        roi.setdefault("geometry_locked", False)
    return roi


def _roi_visibility_status(roi: dict) -> str:
    """行级可见性状态列（0056）：unclaimed / granted / private。

    判定权威在 annotation_access.is_unclaimed / annotation_grants；本列只是
    查询与审计报表的 aid（0056 迁移一次性回填存量，运行期由本函数与授权
    函数维护）。
    """
    if annotation_access.is_unclaimed(roi):
        return "unclaimed"
    return "private"


def _insert_roi(cur, roi: dict, rid: str):
    """插入一条 roi：data 存权威 dict，离散列镜像，返回 insert_seq。

    P2（合同 §3.1/R-08）：slide_id（权威）+ slide（名称快照）双列写入；
    未解析到资产（无 slides 行）时 slide_id 保持 NULL = unresolved。
    """
    now = roi.get("updated_at") or roi.get("ts") or time.time()
    cur.execute(
        "INSERT INTO rois "
        "(id, token, slide, annotation_id, label, type, geom, size_mm, shared, "
        " note, deleted, owner_user_id, created_at, updated_at, data, "
        " visibility_status, client_action_id, slide_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, to_timestamp(%s), "
        "to_timestamp(%s), %s, %s, %s, %s) RETURNING insert_seq",
        (
            rid, roi.get("token"), roi.get("slide"), roi.get("annotation_id"),
            roi.get("label", ""), roi.get("type", "rect"),
            psycopg.types.json.Jsonb(_geom_of(roi)),
            roi.get("size_mm", 0.0), bool(roi.get("shared", False)),
            roi.get("note", ""), bool(roi.get("deleted", False)),
            roi.get("owner_user_id"), now, now,
            psycopg.types.json.Jsonb(roi),
            _roi_visibility_status(roi), roi.get("client_action_id"),
            roi.get("slide_id") or None,
        ),
    )
    return cur.fetchone()["insert_seq"]


def _update_roi_row(cur, rid: str, roi: dict):
    """更新一条 roi（data + 离散镜像列）。"""
    now = roi.get("updated_at") or time.time()
    cur.execute(
        "UPDATE rois SET geom=%s, size_mm=%s, shared=%s, note=%s, deleted=%s, "
        "updated_at=to_timestamp(%s), data=%s, annotation_id=%s, label=%s, "
        "type=%s, owner_user_id=%s WHERE id=%s",
        (
            psycopg.types.json.Jsonb(_geom_of(roi)),
            roi.get("size_mm", 0.0), bool(roi.get("shared", False)),
            roi.get("note", ""), bool(roi.get("deleted", False)), now,
            psycopg.types.json.Jsonb(roi), roi.get("annotation_id"),
            roi.get("label", ""), roi.get("type", "rect"),
            roi.get("owner_user_id"), rid,
        ),
    )


def _slide_id_read_compat(roi: dict, col_slide_id) -> dict:
    """读兼容（hotfix 2026-10-19）：JSON data 缺 ``slide_id`` 键时以离散列
    权威回填（仅输出副本，不回写存储）。

    生产实测存在「``rois.slide_id`` 列已落 ID、JSON 文档缺键」的分裂态行
    （写路径收口前的历史行）。读侧一律以列为准回填，保证调用方（分组/
    过滤/归属比较）拿到的 ``slide_id`` 与权威列一致；JSON 已有键时不覆盖。
    """
    if col_slide_id and not roi.get("slide_id"):
        roi["slide_id"] = col_slide_id
    return roi


def _roi_out(roi: dict, index=None, shared=None) -> dict:
    """ROI 导出副本：统一补 index/shared/note 兼容字段（与 json 一致）。

    tombstone（deleted=true）只保留最小字段；非 tombstone 补 review_status 与
    geometry_locked（旧行缺省 false，§6.3）。
    升级 C（§6.3-1）：旧 rect 只有 side_px → 读时归一 w=h=side_px（仅输出
    副本，不改存储；不批量改 annotation_id/revision/change_seq 等）。
    """
    if roi.get("deleted"):
        out = {
            "annotation_id": roi.get("annotation_id"),
            "slide": roi.get("slide"),
            "token": roi.get("token"),
            "revision": int(roi.get("revision") or 1),
            "deleted": True,
            "deleted_at": roi.get("deleted_at"),
            "change_seq": roi.get("change_seq"),
            "type": "annotation",
        }
        if index is not None:
            out["index"] = index
        return out
    out = _geometry_lock_read(dict(_rect_read_compat(dict(roi))))
    if index is not None:
        out["index"] = index
    if shared is not None:
        out["shared"] = bool(shared)
    out["note"] = roi.get("note", "")
    out.setdefault("review_status", "none")
    # Stage 3c-2：历史 AI 标注（source=ai 但无 provenance）输出 partial 标记
    if roi.get("source") == "ai" and not isinstance(roi.get("provenance"), dict):
        out["provenance"] = {"partial": True}
    return out


def _fetch_live_rois_locked(cur, token):
    """按 token 取出未删除 ROI 并锁行，保证 revision CAS 与并发更新串行。"""
    cur.execute(
        "SELECT id, data FROM rois WHERE token=%s AND NOT deleted "
        "ORDER BY insert_seq FOR UPDATE",
        (token,),
    )
    return cur.fetchall()


def _fetch_token_rows(cur, token):
    """按 token 取全部 ROI 行（含 tombstone，insert_seq 序）——index 语义
    （token 内含 tombstone 的数组位置）与幂等回读定位用。"""
    cur.execute(
        "SELECT data FROM rois WHERE token=%s ORDER BY insert_seq",
        (token,),
    )
    return cur.fetchall()


def _check_cas(roi, expected_revision):
    """Stage 3c-1 CAS：expected_revision 提供且与当前 revision 不符 → 抛 RevisionConflict。

    引用本模块（pg）的 RevisionConflict，保证 postgres 模式下异常类一致。
    """
    if expected_revision is None:
        return
    cur = int(roi.get("revision") or 1)
    if int(expected_revision) != cur:
        raise RevisionConflict(cur)


def _append_history(roi, op=None, actor=None):
    """Stage 3c-1 修改历史：把当前快照 append 进 roi['history']，上限 20，丢最旧。
    在 update/tombstone 修改**之前**调用。pg 存 data jsonb 内（同 roi dict）。

    §6.3 几何锁：快照补 geometry_locked 状态；锁/解锁（op=lock/unlock）额外
    记录操作者（actor；user:<uid> 或 "visitor"——不含访客哈希，可直接对外）。
    """
    snap = {
        "geom": {k: roi[k] for k in _GEOM_KEYS if k in roi},
        "note": roi.get("note", ""),
        "label": roi.get("label", ""),
        "revision": int(roi.get("revision") or 1),
        "ts": roi.get("ts"),
        "geometry_locked": bool(roi.get("geometry_locked")),
    }
    if op:
        snap["op"] = op
    if actor:
        snap["actor"] = actor
    hist = roi.setdefault("history", [])
    hist.append(snap)
    if len(hist) > 20:
        del hist[: len(hist) - 20]


def _bump_change_seq(cur, slide, token, annotation_id, op, slide_id=None):
    """写一条 change_log，返回全局单调 seq（作为该 roi 的 change_seq）。

    P2（合同 §3.1/R-08）：slide_id（权威）+ slide（名称快照）双列。
    """
    cur.execute(
        "INSERT INTO change_log (slide, token, annotation_id, op, slide_id) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING seq",
        (slide, token, annotation_id, op, slide_id or None),
    )
    return cur.fetchone()["seq"]


# --------------------------------------------------------------------------- #
# 分享（shares）
# --------------------------------------------------------------------------- #
def _resolve_share_slide_row(cur, name):
    """分享成员名 → slide_id（P2 收口 / R-04；P1-B2 偏差 #4）。

    只解析既有行：slides.legacy_filename 冻结映射命中才返回 slide_id；
    **无行不再懒建**（分享创建仅接受能解析到已存在资产的名/ID——由调用方
    校验后传入；本函数返回 None 表示不可解析，调用方必须拒绝而非跳过）。
    """
    if not isinstance(name, str) or not name:
        return None
    cur.execute("SELECT slide_id FROM slides WHERE legacy_filename=%s",
                (name,))
    row = cur.fetchone()
    return row["slide_id"] if row is not None else None


def create_share(slides, expires_hours, roi_sizes=None, permissions=None,
                 creator_user_id=None, requester_role=None, rect_policy=None,
                 slide_ids=None):
    """创建分享：生成 token、写入并返回 share dict（含 token/roi_sizes/rect_policy）。

    升级 C（§6.4）：rect_policy ∈ preset_only|custom；缺省 preset_only
    （新建分享不显式选择时不放宽为 custom）。

    P1-B2（R-04）→ P2 收口（偏差 #4）：INSERT shares 后把成员逐个映射到
    slide_id 写入 share_slides（position=数组序，ON CONFLICT DO NOTHING）。
    ``slide_ids`` 显式给出（新客户端）时直接使用（快照名回查 legacy_filename）；
    名数组走冻结别名解析。**无法解析到已存在资产的名/ID → ValueError（400，
    指明哪一个）**——不再懒建行（「先建分享后放文件」旧流程收口）。shares.
    slides JSONB 照写（兼容快照——列表/claimed 展示仍读它，授权判定不再
    参与，见 share_server._require_slide / slide_store.authorize_read）。
    """
    _reject_guest_write(requester_role)
    roi_sizes_norm = _normalize_roi_sizes(roi_sizes)
    perms = _normalize_permissions(permissions)
    policy = _normalize_rect_policy(rect_policy)
    creator = creator_user_id or None
    token = secrets.token_urlsafe(18)
    now = time.time()
    expires_at = now + float(expires_hours) * 3600.0

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO shares "
                    "(token, slides, permissions, roi_sizes, rect_policy, "
                    " expires_at, revoked, creator_user_id) "
                    "VALUES (%s,%s,%s,%s,%s, to_timestamp(%s), FALSE, %s)",
                    (token, psycopg.types.json.Jsonb(list(slides)),
                     psycopg.types.json.Jsonb(list(perms)),
                     psycopg.types.json.Jsonb(list(roi_sizes_norm)),
                     policy, expires_at, creator),
                )
                # R-04：share_slides ID 关系（授权判定唯一来源）。
                # P2 收口：slide_ids（新客户端）优先；名走冻结别名解析；
                # 解析不到已存在资产 → 整体拒绝（ValueError → 400，指明哪个）。
                resolved = []
                ids = list(slide_ids) if slide_ids else [None] * len(list(slides))
                for s, sid_in in zip(list(slides), ids):
                    sid = sid_in
                    if sid is None:
                        if not isinstance(s, str) or not s:
                            raise ValueError("分享成员含非法切片名：%r" % (s,))
                        sid = _resolve_share_slide_row(cur, s)
                        if sid is None:
                            raise ValueError("切片不存在，无法加入分享: %s" % s)
                    else:
                        cur.execute(
                            "SELECT slide_id FROM slides WHERE slide_id=%s",
                            (sid,))
                        if cur.fetchone() is None:
                            raise ValueError("切片不存在，无法加入分享: %s"
                                             % sid)
                    resolved.append((s, sid))
                for pos, (s, sid) in enumerate(resolved):
                    cur.execute(
                        "INSERT INTO share_slides (token, slide_id, position) "
                        "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (token, sid, pos))
        return {
            "slides": list(slides),
            "created_at": now,
            "expires_at": expires_at,
            "revoked": False,
            "token": token,
            "roi_sizes": list(roi_sizes_norm),
            "rect_policy": policy,
            "permissions": list(perms),
            "creator_user_id": creator,
        }
    finally:
        conn.close()


_SHARE_SEL = (
    "token, slides, permissions, roi_sizes, "
    "COALESCE(rect_policy, 'preset_only') AS rect_policy, "
    "extract(epoch from expires_at)::float8 AS expires_at, revoked, "
    "creator_user_id, extract(epoch from created_at)::float8 AS created_at"
)


def _fetch_share(cur, token):
    cur.execute("SELECT " + _SHARE_SEL + " FROM shares WHERE token=%s", (token,))
    return cur.fetchone()


def get_share(token):
    """获取有效分享；不存在/已撤销/已过期返回 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                share = _fetch_share(cur, token)
        if share is None:
            return None
        if not _is_active(share):
            return None
        out = dict(share)
        out["token"] = token
        out["roi_sizes"] = _share_roi_sizes(share)
        out["rect_policy"] = _share_rect_policy(share)
        out["permissions"] = _share_permissions(share)
        return out
    finally:
        conn.close()


def list_shares():
    """返回全部分享（含 status/roi_sizes/rect_policy 字段），按 created_at 倒序。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT " + _SHARE_SEL +
                            " FROM shares ORDER BY created_at DESC, token")
                rows = cur.fetchall()
        items = []
        for row in rows:
            sh = dict(row)
            out = dict(sh)
            out["status"] = _status_of(sh)
            out["roi_sizes"] = _share_roi_sizes(sh)
            out["rect_policy"] = _share_rect_policy(sh)
            out["permissions"] = _share_permissions(sh)
            items.append(out)
        return items
    finally:
        conn.close()


def revoke_share(token):
    """撤销分享，返回是否成功。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("UPDATE shares SET revoked=TRUE WHERE token=%s", (token,))
                return cur.rowcount > 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 认领（grants）
# --------------------------------------------------------------------------- #
def claim_share(token, user_id, permissions=None):
    """user 认领分享链接（幂等）。返回 grant dict。

    权限夹在当前分享权限子集内；缺省使用分享权限（不是全局 DEFAULT）。
    """
    if not isinstance(token, str) or not token:
        raise ValueError("token 不能为空")
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id 不能为空")

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                share = _fetch_share(cur, token)
                allowed = (_share_permissions(share) if share is not None
                           else list(DEFAULT_PERMISSIONS))
                # 幂等：同 token + 同 user 且未失效（active ⇔ json revoked_at is None）
                cur.execute(
                    "SELECT id, token, user_id, permissions, "
                    "extract(epoch from claimed_at)::float8 AS claimed_at, active "
                    "FROM grants WHERE token=%s AND user_id=%s AND active "
                    "FOR UPDATE",
                    (token, user_id),
                )
                existing = cur.fetchone()
                if existing is not None:
                    g = dict(existing)
                    g["grant_id"] = g["id"]
                    g["share_token"] = g["token"]
                    g["revoked_at"] = None
                    if permissions is not None:
                        perms = _cap_claim_permissions(permissions, allowed)
                    else:
                        perms = [p for p in _grant_permissions_of(g) if p in allowed]
                        if not perms:
                            perms = list(allowed)
                    if list(g.get("permissions") or []) != list(perms):
                        cur.execute(
                            "UPDATE grants SET permissions=%s WHERE id=%s",
                            (psycopg.types.json.Jsonb(list(perms)), g["id"]),
                        )
                        g["permissions"] = list(perms)
                    return _grant_out(g)
                perms = _cap_claim_permissions(permissions, allowed)
                gid = "grt_" + secrets.token_urlsafe(8)
                now = time.time()
                cur.execute(
                    "INSERT INTO grants (id, token, user_id, permissions, "
                    "claimed_at, active) VALUES (%s,%s,%s,%s, to_timestamp(%s), TRUE)",
                    (gid, token, user_id, psycopg.types.json.Jsonb(list(perms)), now),
                )
                g = {
                    "grant_id": gid,
                    "user_id": user_id,
                    "share_token": token,
                    "permissions": list(perms),
                    "claimed_at": now,
                    "revoked_at": None,
                }
                return _grant_out(g)
    finally:
        conn.close()


def claimed_active_slides_for_user(user_id, permission=None):
    """返回该 user 认领过的、且对应 share 仍 active 的切片名集合。

    permission 若给出，只计入 grant 含该权限的切片。
    """
    if not user_id:
        return set()
    if permission is not None and permission not in (
            PERMISSION_VIEW, PERMISSION_ANNOTATE, PERMISSION_DOWNLOAD):
        return set()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT token, permissions FROM grants "
                    "WHERE user_id=%s AND active", (user_id,)
                )
                grants = cur.fetchall()
                out = set()
                for g in grants:
                    if permission is not None and permission not in _grant_permissions_of(g):
                        continue
                    tok = g["token"]
                    share = _fetch_share(cur, tok)
                    if share is None or not _is_active(share):
                        continue
                    for s in share.get("slides") or []:
                        if isinstance(s, str):
                            out.add(s)
                return out
    finally:
        conn.close()


def list_grants_for_user(user_id):
    """返回该 user 的全部 grant（含已失效，附 share_active 标志）。供调试/审计。"""
    if not user_id:
        return []
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, token, user_id, permissions, "
                    "extract(epoch from claimed_at)::float8 AS claimed_at, active "
                    "FROM grants WHERE user_id=%s ORDER BY claimed_at DESC",
                    (user_id,),
                )
                rows = cur.fetchall()
                out = []
                for row in rows:
                    g = dict(row)
                    g["grant_id"] = g["id"]
                    g["share_token"] = g["token"]
                    g["revoked_at"] = None
                    share = _fetch_share(cur, g["share_token"])
                    g["share_active"] = bool(
                        share is not None and _is_active(share))
                    out.append(_grant_out(g))
        out.sort(key=lambda x: x.get("claimed_at", 0), reverse=True)
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 切片可见性显式授权（slide_view_grants；review P0 2026-09-05 读隔离）
#
# 背景：owner 不再默认可见全部切片——读隔离模型与 user 一致（自己的 ∪
# public ∪ 认领 view ∪ 本节直授）。不复用 share 认领（grants FK 绑定
# shares.token，复用需伪造内部 share：污染分享列表、撤 share 即静默收回、
# 权限被 share permissions 夹逼、有 expires_at TTL 陷阱）。本表持久、无
# TTL、(slide_name, user_id) 主键天然幂等；授权可先于 slides meta 行存在
# （无外键，同 rois.slide 理由，见 migrations/0034 注释）。当前唯一授予
# 方向是管理台给 owner 自授权（app.py /api/admin/v1/slides/*）。
#
# 资产生命周期（升级 B R7，0035 起）：行上追加 slide_id（授权建立时的资产
# 代）。读取侧（slide_view_grants_for_user）要求行上 slide_id 与 slides 行
# 当前值 IS NOT DISTINCT FROM 匹配——孤儿授权（双方皆 NULL）语义不变，而
# 「删除 → 同名再上传」替换资产后，即使行未被清理也不再匹配新内容；删除
# 路径（revoke_slide_view_grants_for_slide）按名 + slide_id 清理行。
# --------------------------------------------------------------------------- #
def grant_slide_view(user_id, slide_name, ttl_seconds, granted_by=None,
                     slide_id=None):
    """建立 view 授权（幂等 + 显式资产生代重绑带；2026-10-08 §3 起带到期时间）。

    返回 {"already_granted", "granted_at", "expires_at"}。行不存在 → 插入
    （绑定当前 slide_id，expires_at = now()+ttl）；已存在同名行 → 幂等成功
    （保留首次 granted_at/granted_by/expires_at），但当资产生代失配（行上
    slide_id ≠ 本次给定值）时**更新为当前 slide_id**——升级 B R7 失效语义的
    另一半：同名替换后旧授权不自动生效，需要重新添加；重新添加即显式重绑
    当前资产生代（COALESCE 允许孤儿切片以 NULL 授权行保持 NULL）。
    user_id/slide_name 需非空字符串。

    0080 起 expires_at NOT NULL：``ttl_seconds`` 是**必填**正数（round-2
    收紧：无缺省——任何调用方都不可能因漏传而造出「永久」授权）。该函数
    不再有生产写入方（旧 visibility 端点已退役），仅供测试/工具构造授权
    （测试可显式传长 TTL 表达「实质不过期」）；生产临时查看走
    start/end_slide_view_grant_timed。
    """
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id 不能为空")
    if not isinstance(slide_name, str) or not slide_name:
        raise ValueError("slide_name 不能为空")
    try:
        ttl = float(ttl_seconds)
    except (TypeError, ValueError):
        raise ValueError("ttl_seconds 需为正数（必填，秒）")
    if ttl <= 0:
        raise ValueError("ttl_seconds 需为正数（必填，秒）")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # RETURNING (xmax = 0)：xmax=0 仅在真正 INSERT 的新行上成立
                # （ON CONFLICT DO UPDATE 走更新路径 rowcount 同为 1，不能
                # 用 rowcount 区分插入/幂等更新）。
                cur.execute(
                    "INSERT INTO slide_view_grants (slide_name, user_id, "
                    "granted_by, slide_id, expires_at) "
                    "VALUES (%s,%s,%s,%s, now() + (%s * interval '1 second')) "
                    "ON CONFLICT (slide_name, user_id) DO UPDATE SET slide_id = "
                    "COALESCE(EXCLUDED.slide_id, slide_view_grants.slide_id) "
                    "RETURNING (xmax = 0) AS inserted, extract(epoch from "
                    "granted_at)::float8 AS granted_at, "
                    "extract(epoch from expires_at)::float8 AS expires_at",
                    (slide_name, user_id, granted_by or None,
                     slide_id or None, ttl),
                )
                row = cur.fetchone()
                inserted = bool(row["inserted"])
                if not inserted:
                    # 幂等重放：granted_at/expires_at 语义保留「首次授权」值
                    cur.execute(
                        "SELECT extract(epoch from granted_at)::float8 AS "
                        "granted_at, extract(epoch from expires_at)::float8 "
                        "AS expires_at FROM slide_view_grants "
                        "WHERE slide_name=%s AND user_id=%s",
                        (slide_name, user_id),
                    )
                    row = cur.fetchone()
        return {
            "user_id": user_id,
            "slide_name": slide_name,
            "granted_by": granted_by or None,
            "already_granted": not inserted,
            "granted_at": row["granted_at"] if row else None,
            "expires_at": row["expires_at"] if row else None,
        }
    finally:
        conn.close()


def start_slide_view_grant_timed(user_id, slide_name, granted_by=None,
                                 slide_id=None, ttl_seconds=3600.0):
    """管理员临时查看「开启」（2026-10-08 §3.1；单事务幂等）。

    - 已有 ``expires_at > now()`` 的授权 → 原样返回（``started=False``，
      **不续期**——重复开启返回原到期，天然幂等）；
    - 否则刷新授权窗口：``granted_at=now(), expires_at=now()+ttl,
      granted_by, slide_id``（过期行被重新开启 = 新窗口）。

    行寻址以 (slide_id, user_id) 唯一索引（0067）为权威：同一主体对同一
    slide_id 至多一行（历史行可能以 legacy 名或 slide_id 为 slide_name 键，
    混合键形态在锁内归一刷新，绝不插第二行）。

    返回 ``{"started", "granted_at", "expires_at"}``（epoch 秒）。
    """
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id 不能为空")
    if not isinstance(slide_name, str) or not slide_name:
        raise ValueError("slide_name 不能为空")
    try:
        ttl = float(ttl_seconds)
    except (TypeError, ValueError):
        raise ValueError("ttl_seconds 需为数值")
    if ttl <= 0:
        raise ValueError("ttl_seconds 需为正数")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # 权威行：该主体对该 slide_id 的行（任意键形态）
                if slide_id:
                    cur.execute(
                        "SELECT slide_name, extract(epoch from granted_at)::"
                        "float8 AS granted_at, extract(epoch from expires_at)"
                        "::float8 AS expires_at FROM slide_view_grants "
                        "WHERE user_id=%s AND slide_id=%s FOR UPDATE",
                        (user_id, slide_id))
                    row = cur.fetchone()
                    if row is not None:
                        if row["expires_at"] > time.time():
                            return {"started": False,
                                    "granted_at": row["granted_at"],
                                    "expires_at": row["expires_at"]}
                        # 过期：原地刷新窗口（新 granted_at/expires_at；
                        # slide_name 归一为当前键——历史行可能以 slide_id
                        # 字符串为名键，按名读路径依赖与 legacy 名一致）
                        cur.execute(
                            "UPDATE slide_view_grants SET slide_name=%s, "
                            "granted_by=%s, slide_id=%s, granted_at=now(), "
                            "expires_at=now() + (%s * interval '1 second') "
                            "WHERE user_id=%s AND slide_id=%s RETURNING "
                            "extract(epoch from granted_at)::float8 AS "
                            "granted_at, extract(epoch from expires_at)::"
                            "float8 AS expires_at",
                            (slide_name, granted_by or None, slide_id, ttl,
                             user_id, slide_id))
                        r2 = cur.fetchone()
                        return {"started": True,
                                "granted_at": r2["granted_at"],
                                "expires_at": r2["expires_at"]}
                # 无 (slide_id, user) 行：按 (slide_name, user) upsert
                # （孤儿/历史 NULL-ID 形态；命中唯一索引冲突不可达——上面
                # 已按 slide_id 归一）
                cur.execute(
                    "INSERT INTO slide_view_grants "
                    "(slide_name, user_id, granted_by, slide_id, granted_at, "
                    " expires_at) VALUES (%s,%s,%s,%s, now(), "
                    " now() + (%s * interval '1 second')) "
                    "ON CONFLICT (slide_name, user_id) DO UPDATE SET "
                    "granted_by=EXCLUDED.granted_by, "
                    "slide_id=COALESCE(EXCLUDED.slide_id, "
                    "slide_view_grants.slide_id), granted_at=now(), "
                    "expires_at=EXCLUDED.expires_at "
                    "RETURNING extract(epoch from granted_at)::float8 AS "
                    "granted_at, extract(epoch from expires_at)::float8 AS "
                    "expires_at",
                    (slide_name, user_id, granted_by or None,
                     slide_id or None, ttl))
                r3 = cur.fetchone()
                # 本分支必然开新窗口（新行或同键过期行刷新）
                return {"started": True,
                        "granted_at": r3["granted_at"],
                        "expires_at": r3["expires_at"]}
    finally:
        conn.close()


def end_slide_view_grant(user_id, slide_name, slide_id=None):
    """管理员临时查看「结束」（2026-10-08 §3.1；幂等）。

    把该主体对该切片**未到期**的授权 ``expires_at`` 置为 now()（已到期/无行
    不动）。按 slide_id（权威，0067 唯一索引）命中，缺省回退 slide_name。
    返回 "ended"（确有未到期授权被结束）或 "none"（幂等重放）。
    """
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id 不能为空")
    if not isinstance(slide_name, str) or not slide_name:
        raise ValueError("slide_name 不能为空")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if slide_id:
                    cur.execute(
                        "UPDATE slide_view_grants SET expires_at=now() "
                        "WHERE user_id=%s AND (slide_id=%s OR slide_name=%s) "
                        "AND expires_at > now()",
                        (user_id, slide_id, slide_name))
                else:
                    cur.execute(
                        "UPDATE slide_view_grants SET expires_at=now() "
                        "WHERE slide_name=%s AND user_id=%s "
                        "AND expires_at > now()", (slide_name, user_id))
                return "ended" if cur.rowcount > 0 else "none"
    finally:
        conn.close()


def active_slide_view_grants_for_user(user_id):
    """主体当前**未到期**的 slide_view_grants：{slide_id: expires_at(epoch)}。

    /api/slides 的 temporary_view_expires_at 标注与 AI run grant 到期钳制
    （§3.2/§3.3）共用；只返回行上 slide_id 非空的授权。
    """
    if not user_id:
        return {}
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT slide_id, extract(epoch from expires_at)::float8 "
                    "AS expires_at FROM slide_view_grants "
                    "WHERE user_id=%s AND slide_id IS NOT NULL "
                    "AND expires_at > now()", (user_id,))
                return {r["slide_id"]: float(r["expires_at"])
                        for r in cur.fetchall()}
    finally:
        conn.close()


def revoke_slide_view_grants_for_slide(slide_name, slide_id=None):
    """资产生命周期收口（升级 B R7）：删除某切片的全部 view 授权行。

    按 legacy 名删除全部主体的授权；slide_id 给出时**同事务**再按资产生代
    清一遍残留（防御同名行上残留旧代 slide_id 的形态）。切片文件删除
    （app.py api_slide_delete）在 unlink 前调用——「删除 → 同名再上传」后
    旧授权不自动生效（失效语义），需要重新添加。返回删除的行数。
    """
    if not isinstance(slide_name, str) or not slide_name:
        raise ValueError("slide_name 不能为空")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "DELETE FROM slide_view_grants WHERE slide_name=%s",
                    (slide_name,),
                )
                deleted = cur.rowcount
                if slide_id:
                    cur.execute(
                        "DELETE FROM slide_view_grants WHERE slide_id=%s "
                        "AND slide_name <> %s",
                        (slide_id, slide_name),
                    )
                    deleted += cur.rowcount
                return deleted
    finally:
        conn.close()


def slide_view_grants_for_user(user_id):
    """返回该主体被显式授权可见的切片名集合（无则空集）。

    升级 B R7：要求授权行资产生代（slide_id）与 slides 行当前值一致
    （IS NOT DISTINCT FROM，双方皆 NULL 的孤儿授权照常生效）——同名资产
    替换后旧授权不再匹配新内容（配合删除路径的按名清理，失效语义双保险）。
    2026-10-08 §3.2：只认未到期行（expires_at > now()，0080 起）。
    """
    if not user_id:
        return set()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT g.slide_name FROM slide_view_grants g "
                    "LEFT JOIN slides s ON s.legacy_filename = g.slide_name "
                    "WHERE g.user_id=%s "
                    "AND g.expires_at > now() "
                    "AND g.slide_id IS NOT DISTINCT FROM s.slide_id",
                    (user_id,),
                )
                return {r["slide_name"] for r in cur.fetchall()}
    finally:
        conn.close()


def list_slide_view_grants():
    """返回全部 view 授权行（inventory 标注管理员临时查看状态用）。

    行形态 {slide_name, user_id, granted_by, granted_at(epoch), slide_id,
    expires_at(epoch)}；按授权时间降序。表不存在前（迁移未跑）调用方会拿到
    编程错误——ensure_schema 在 app 启动期 fail-fast，运行期表必然存在。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT slide_name, user_id, granted_by, slide_id, "
                    "extract(epoch from granted_at)::float8 AS granted_at, "
                    "extract(epoch from expires_at)::float8 AS expires_at "
                    "FROM slide_view_grants ORDER BY granted_at DESC, "
                    "slide_name, user_id",
                )
                return [
                    {
                        "slide_name": r["slide_name"],
                        "user_id": r["user_id"],
                        "granted_by": r["granted_by"],
                        "slide_id": r["slide_id"],
                        "granted_at": r["granted_at"],
                        "expires_at": r["expires_at"],
                    }
                    for r in cur.fetchall()
                ]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 标注（rois）
# --------------------------------------------------------------------------- #
def add_roi(token, slide, label, type="rect", size_mm=0.0, shared=False, note="", visitor=None,
            source=None, created_by_session_id=None, _effect_key=None, owner_user_id=None,
            requester_role=None, provenance=None, client_action_id=None,
            slide_id=None, geometry_locked=False, **geom):
    """为 token 的 share 添加一条标注；统一入口，支持 rect/arrow/freehand。

    语义与 json 完全一致（含 WAL effect_key 幂等、index 语义、source 推断）。
    0056：client_action_id（客户端幂等键）——有 owner 时按
    (owner_user_id, client_action_id) 唯一约束去重，重复提交返回原标注
    （唯一索引兜底并发，撞索引时回读原行）。
    P2（合同 §3.1/R-08）：``slide_id`` 显式传入（id_bundle 资产/已解析的
    调用方）或按 legacy 名解析（slides.legacy_filename 冻结映射）；解析不到
    保持 NULL = unresolved。rois/change_log 双写 slide_id + slide 快照。
    §6.3：``geometry_locked``（可选，缺省 False）——草稿保存时带锁；AI 写
    路径不传该参 → 恒 false。
    2026-10-08 缺陷修复（R-04）：slide ∈ share 成员判定改 share_slides
    ID 关系（slide_id 已知时；与读通道同源），仅无 ID 的历史名行回退
    shares.slides JSONB 名快照——ID-only 资产（无 legacy 名）此前被名快照
    误拒 400「slide not in share」。
    """
    _reject_guest_write(requester_role)
    if type not in ROI_TYPES:
        raise ValueError("未知标注类型")
    if not isinstance(label, str):
        raise ValueError("请填写用户名或标签")
    label = label.strip()
    if not label:
        raise ValueError("请填写用户名或标签")
    note_clean = _clean_note(note)

    geom_full = dict(geom)
    geom_full["size_mm"] = size_mm
    norm = _validate_geom(type, geom_full)
    norm["type"] = type

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                is_admin = (token == ADMIN_TOKEN)
                if not is_admin:
                    share = _fetch_share(cur, token)
                    if share is None or not _is_active(share):
                        raise ValueError("share invalid")
                    # 2026-10-08 缺陷修复（R-04 同口径）：slide ∈ share 判定
                    # 优先走 share_slides(token, slide_id) ID 关系——与读通道
                    # （share_server._require_slide*）及路由层同一授权来源；
                    # ID-only 资产无 legacy 名（slide 为 None/空），旧
                    # shares.slides JSONB 名快照判定必误拒「slide not in
                    # share」。无 slide_id（未解析到资产的历史名行）才回退
                    # 名快照兜底。
                    member_sid = slide_id
                    if not member_sid:
                        member_sid = _slide_id_of_name(cur, slide)
                    if member_sid:
                        cur.execute(
                            "SELECT 1 FROM share_slides "
                            "WHERE token=%s AND slide_id=%s LIMIT 1",
                            (token, member_sid))
                        if cur.fetchone() is None:
                            raise ValueError("slide not in share")
                    elif slide not in (share.get("slides") or []):
                        raise ValueError("slide not in share")
                # 0056 幂等：client_action_id（有 owner 时）已落 → 复用返回
                if client_action_id and (owner_user_id or _OWNER_USER_ID):
                    eff_owner = owner_user_id or _OWNER_USER_ID
                    cur.execute(
                        "SELECT data FROM rois WHERE NOT deleted "
                        "AND owner_user_id=%s AND client_action_id=%s "
                        "ORDER BY insert_seq",
                        (eff_owner, client_action_id),
                    )
                    hit = cur.fetchone()
                    if hit is not None:
                        all_rows = _fetch_token_rows(cur, token)
                        idx = next(
                            (i for i, row in enumerate(all_rows)
                             if row["data"].get("annotation_id") ==
                             hit["data"].get("annotation_id")),
                            len(all_rows) - 1)
                        return _roi_out(hit["data"], index=idx,
                                        shared=_roi_shared_compat(hit["data"]))
                # WAL 幂等：effect_key 已落 → 复用返回
                if _effect_key:
                    cur.execute(
                        "SELECT data FROM rois WHERE NOT deleted "
                        "AND data->>'effect_key'=%s ORDER BY insert_seq",
                        (_effect_key,),
                    )
                    # 需在同 token 内定位 index（含 tombstone，同 json 的 unfiltered）
                    cur.execute(
                        "SELECT data FROM rois WHERE token=%s ORDER BY insert_seq",
                        (token,),
                    )
                    all_rows = cur.fetchall()
                    idx = None
                    hit = None
                    for i, row in enumerate(all_rows):
                        if row["data"].get("effect_key") == _effect_key and \
                                not row["data"].get("deleted"):
                            idx = i
                            hit = row["data"]
                            break
                    if hit is not None:
                        return _roi_out(hit, index=(idx if idx is not None else
                                                     len(all_rows) - 1),
                                        shared=_roi_shared_compat(hit))
                now = time.time()
                src = source if source in ("ai", "human") else (
                    "ai" if (is_admin and not shared) else "human")
                # P2：slide_id 双写解析（显式优先；名解析失败保持 NULL）
                eff_slide_id = slide_id or _slide_id_of_name(cur, slide)
                # 2026-10-08 缺陷修复：ID-only 资产无名快照（slide 为 None/""）
                # 时按 slide_id 回查名称快照（legacy → original_filename，
                # 与 /api/annotation、/api/share/create 口径一致）——保证
                # rois.slide / change_log.slide（NOT NULL）非空。
                if not slide and eff_slide_id:
                    cur.execute(
                        "SELECT legacy_filename, original_filename "
                        "FROM slides WHERE slide_id=%s", (eff_slide_id,))
                    _snap = cur.fetchone()
                    if _snap is not None:
                        slide = (_snap["legacy_filename"]
                                 or _snap["original_filename"] or "")
                # index = 该 token 全部 roi（含 tombstone）中新增前的数量
                cur.execute("SELECT count(*) FROM rois WHERE token=%s", (token,))
                total = int(cur.fetchone()["count"])
                roi = {
                    "token": token,
                    "slide": slide,
                    "slide_id": eff_slide_id or None,
                    "label": label,
                    "ts": now,
                    "shared": bool(shared),
                    "note": note_clean,
                    "visitor": visitor or "",
                    "annotation_id": str(uuid.uuid4()),
                    "source": src,
                    "created_by_session_id": created_by_session_id or "",
                    "revision": 1,
                    "updated_at": now,
                    "deleted": False,
                    "owner_user_id": owner_user_id or _OWNER_USER_ID or None,
                    # Stage 3c-1：AI 新写入默认 pending 待审；人工标注 none
                    "review_status": "pending" if src == "ai" else "none",
                    # §6.3 几何位置锁（缺省 false；显式带上避免读侧猜键）
                    "geometry_locked": bool(geometry_locked),
                }
                if _effect_key:
                    roi["effect_key"] = _effect_key
                if client_action_id:
                    roi["client_action_id"] = str(client_action_id)
                # Stage 3c-2：AI 溯源子对象（仅 AI 写入，且仅当传入非空 dict 才落）
                if src == "ai" and isinstance(provenance, dict) and provenance:
                    roi["provenance"] = dict(provenance)
                roi.update(norm)
                roi["change_seq"] = _bump_change_seq(
                    cur, slide, token, roi["annotation_id"], "add",
                    slide_id=eff_slide_id)
                rid = "roi_" + secrets.token_urlsafe(10)
                try:
                    _insert_roi(cur, roi, rid)
                except psycopg.errors.UniqueViolation:
                    # 0056 并发兜底：同 (owner, client_action_id) 撞唯一索引
                    # → 事务已失效，回读原行返回（不重复落第二条）
                    if not (client_action_id and roi.get("owner_user_id")):
                        raise
                    c.rollback()
                    with c.cursor() as cur2:
                        cur2.execute(
                            "SELECT data FROM rois WHERE NOT deleted "
                            "AND owner_user_id=%s AND client_action_id=%s "
                            "ORDER BY insert_seq",
                            (roi["owner_user_id"], client_action_id),
                        )
                        hit = cur2.fetchone()
                    if hit is None:
                        raise
                    all_rows = _fetch_token_rows(cur2, token)
                    idx = next(
                        (i for i, row in enumerate(all_rows)
                         if row["data"].get("annotation_id") ==
                         hit["data"].get("annotation_id")),
                        len(all_rows) - 1)
                    return _roi_out(hit["data"], index=idx,
                                    shared=_roi_shared_compat(hit["data"]))
                out = _roi_out(roi)
                out["index"] = total
                out["shared"] = bool(shared)
                return out
    finally:
        conn.close()


def update_roi(token, index, geom=None, note=None, expected_revision=None):
    """更新该 token 下第 index 条 roi 的几何与/或备注。返回更新后的 dict 或 False。

    expected_revision（CAS）：提供且与当前 revision 不符 → 抛 RevisionConflict。
    修改前 append history 快照（上限 20）。
    """
    if geom is not None and not isinstance(geom, dict):
        raise ValueError("geom 需为对象")
    note_clean = "_UNSET_"
    if note is not None:
        note_clean = _clean_note(note)

    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                is_admin = (token == ADMIN_TOKEN)
                if not is_admin:
                    share = _fetch_share(cur, token)
                    if share is None or not _is_active(share):
                        raise ValueError("share invalid")
                same = _fetch_live_rois_locked(cur, token)
                if index < 0 or index >= len(same):
                    return False
                rid = same[index]["id"]
                roi = dict(same[index]["data"])
                # §6.3 几何锁检查先于 CAS（annotation_locked 优先于
                # revision_conflict；已锁时只放行备注等非几何变更）
                _check_geometry_lock(roi, geom is not None)
                _check_cas(roi, expected_revision)  # CAS 在修改前校验
                orig_type = roi.get("type", "rect")
                _append_history(roi)  # 修改历史快照（修改前）
                if geom is not None:
                    geom_full = dict(geom)
                    if orig_type == "rect" and "size_mm" not in geom_full:
                        geom_full["size_mm"] = roi.get("size_mm", 0.0)
                    if orig_type == "rect":
                        # 升级 C：PATCH 兼容性校验（w/h 成对；旧 side_px 编辑
                        # v2 非正方形拒绝），并把 side_px 补丁归一到记录版本
                        _effective_rect_geometry(roi, geom_full)
                        if ("side_px" in geom_full and "w" not in geom_full
                                and "h" not in geom_full
                                and roi.get("geometry_version") == 2):
                            side_val = geom_full.pop("side_px")
                            geom_full["w"] = side_val
                            geom_full["h"] = side_val
                    norm_g = _validate_geom(orig_type, geom_full)
                    norm_g["type"] = orig_type
                    roi.update(norm_g)
                    # 升级 C：v1→v2 升级（成对 w/h）且新几何非正方形时，清掉
                    # 遗留 side_px——非正方形不得保留 max/min 冒充的旧几何字段。
                    if orig_type == "rect" \
                            and norm_g.get("geometry_version") == 2 \
                            and "side_px" not in norm_g:
                        roi.pop("side_px", None)
                if note_clean != "_UNSET_":
                    roi["note"] = note_clean
                roi["revision"] = int(roi.get("revision") or 1) + 1
                roi["change_seq"] = _bump_change_seq(
                    cur, roi.get("slide"), token, roi.get("annotation_id"), "update",
                    slide_id=roi.get("slide_id"))
                roi["updated_at"] = time.time()
                _update_roi_row(cur, rid, roi)
                # index：同 token 非 tombstone 中按插入序
                cur.execute(
                    "SELECT data FROM rois WHERE token=%s AND NOT deleted "
                    "ORDER BY insert_seq", (token,))
                all_rows = cur.fetchall()
                idx = next((i for i, row in enumerate(all_rows)
                            if row["data"].get("annotation_id") ==
                            roi.get("annotation_id")), 0)
                out = _roi_out(roi)
                out["index"] = idx
                out["shared"] = _roi_shared_compat(roi)
                return out
    finally:
        conn.close()


def list_rois(token=None, subject=None, access_context=None):
    """返回 ROI 列表；可按 token 过滤（跳过 tombstone）。

    0056：subject 非 None 时按主体过滤（annotation_access.can_read_annotation）；
    index 保持 pre-filter 位置（token 内非 tombstone 序），不按可见子集重编号。
    hotfix 2026-10-19：``slide_id`` 以离散列权威回填（JSON 缺键的分裂态行
    不再在调用方按 JSON slide_id 过滤时被当 unresolved 丢掉）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if token is not None:
                    cur.execute(
                        "SELECT data, slide_id FROM rois WHERE token=%s "
                        "AND NOT deleted ORDER BY insert_seq", (token,))
                    rows = cur.fetchall()
                else:
                    cur.execute(
                        "SELECT data, slide_id FROM rois WHERE NOT deleted "
                        "ORDER BY insert_seq")
                    rows = cur.fetchall()
        ctx = (access_context if access_context is not None
               else annotation_access.access_context_for(subject)
               if subject is not None else None)
        if token is not None:
            out = []
            for i, row in enumerate(rows):
                if subject is not None and not annotation_access.can_read_annotation(
                        subject, row["data"], ctx):
                    continue
                r = _geometry_lock_read(_rect_read_compat(dict(row["data"])))
                _slide_id_read_compat(r, row["slide_id"])
                r["index"] = i
                r["shared"] = _roi_shared_compat(r)
                r["note"] = r.get("note", "")
                out.append(r)
            out.sort(key=lambda x: x.get("ts", 0), reverse=True)
            return out
        from collections import defaultdict
        counters = defaultdict(int)
        out = []
        for row in rows:
            r_raw = row["data"]
            idx = counters[r_raw["token"]]
            counters[r_raw["token"]] += 1
            if subject is not None and not annotation_access.can_read_annotation(
                    subject, r_raw, ctx):
                continue
            r = _geometry_lock_read(_rect_read_compat(dict(r_raw)))
            _slide_id_read_compat(r, row["slide_id"])
            r["index"] = idx
            r["shared"] = _roi_shared_compat(r)
            r["note"] = r.get("note", "")
            out.append(r)
        out.sort(key=lambda x: x.get("ts", 0), reverse=True)
        return out
    finally:
        conn.close()


def get_roi(token, index):
    """返回该 token 下第 index 条 roi 的 dict 副本（跳过 tombstone）；无则 None。

    hotfix 2026-10-19：``slide_id`` 以离散列权威回填（JSON 缺键的分裂态行
    在归属比较/ID 反解处不再退化为 None）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data, slide_id FROM rois WHERE token=%s "
                    "AND NOT deleted ORDER BY insert_seq", (token,))
                rows = cur.fetchall()
        if index < 0 or index >= len(rows):
            return None
        r = _geometry_lock_read(_rect_read_compat(dict(rows[index]["data"])))
        _slide_id_read_compat(r, rows[index]["slide_id"])
        r["index"] = index
        r["shared"] = _roi_shared_compat(r)
        r["visitor"] = r.get("visitor", "") or ""
        r["note"] = r.get("note", "")
        return r
    finally:
        conn.close()


def get_roi_by_annotation_id(annotation_id):
    """按稳定 annotation_id 取 ROI 完整 dict（含 tombstone）；不存在返回 None。

    hotfix 2026-10-19：``slide_id`` 以离散列权威回填（AI 目标注归属比较
    app._ai_target_clean、评论/审核按 ID 反解切片等读侧不再因 JSON 缺键
    误判为 unresolved）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data, slide_id FROM rois WHERE annotation_id=%s",
                    (annotation_id,))
                row = cur.fetchone()
        if not row:
            return None
        return _slide_id_read_compat(
            _geometry_lock_read(_rect_read_compat(dict(row["data"]))),
            row["slide_id"])
    finally:
        conn.close()


def rehash_plaintext_visitors(token, plaintext_vid, hashed_vid):
    """当前 token 的 share 仍 active 且确有匹配明文时，原子迁移所有相同 visitor。

    同一事务内先 `FOR UPDATE` 锁定 share 并验证未撤销/未过期，再按 id 升序锁定
    ROI，避免与 revoke 并发窗口及认领死锁。无有效 share 或无活 ROI 证明则不改写。
    不 bump revision。返回当前 token 下活 ROI 迁移条数。
    """
    if not token or not plaintext_vid or not hashed_vid:
        return 0
    if not isinstance(plaintext_vid, str) or not isinstance(hashed_vid, str):
        return 0
    if plaintext_vid.startswith("h1.") or hashed_vid == plaintext_vid:
        return 0
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + _SHARE_SEL + " FROM shares WHERE token=%s FOR UPDATE",
                    (token,),
                )
                share = cur.fetchone()
                if share is None or not _is_active(share):
                    return 0
                cur.execute(
                    "SELECT id, token, deleted, data FROM rois "
                    "WHERE data->>'visitor' = %s "
                    "ORDER BY id "
                    "FOR UPDATE",
                    (plaintext_vid,),
                )
                rows = cur.fetchall()
                current_live = 0
                for row in rows:
                    roi = dict(row["data"])
                    if (row.get("token") or roi.get("token")) == token and not (
                        row.get("deleted") or roi.get("deleted")
                    ):
                        current_live += 1
                if current_live == 0:
                    return 0
                for row in rows:
                    roi = dict(row["data"])
                    roi["visitor"] = hashed_vid
                    _update_roi_row(cur, row["id"], roi)
                return current_live
    finally:
        conn.close()


def delete_roi(token, index, expected_revision=None):
    """删除该 token 下第 index 条 ROI（置 tombstone）。返回 (bool, annotation_id|None)。

    expected_revision（CAS）：提供且与当前 revision 不符 → 抛 RevisionConflict。
    tombstone 设 deleted_at + bump revision/change_seq + append history。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                same = _fetch_live_rois_locked(cur, token)
                if index < 0 or index >= len(same):
                    return False, None
                rid = same[index]["id"]
                roi = dict(same[index]["data"])
                if roi.get("deleted"):
                    return False, None
                # §6.3：已锁 → 拒绝删除（annotation_locked 优先于 CAS）；
                # 切片/账号删除等生命周期清理不走本函数，不受此限。
                _check_geometry_lock(roi, True)
                _check_cas(roi, expected_revision)
                _append_history(roi)
                roi["deleted"] = True
                roi["deleted_at"] = time.time()
                roi["revision"] = int(roi.get("revision") or 1) + 1
                roi["change_seq"] = _bump_change_seq(
                    cur, roi.get("slide"), token, roi.get("annotation_id"), "delete",
                    slide_id=roi.get("slide_id"))
                roi["updated_at"] = roi["deleted_at"]
                _update_roi_row(cur, rid, roi)
                return True, roi.get("annotation_id")
    finally:
        conn.close()


def delete_roi_by_annotation_id(annotation_id, expected_revision=None):
    """按稳定 annotation_id 删除（tombstone 语义同 delete_roi）；返回是否成功。

    expected_revision（CAS）：提供且与当前 revision 不符 → 抛 RevisionConflict。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, data FROM rois WHERE annotation_id=%s AND NOT deleted "
                    "FOR UPDATE",
                    (annotation_id,))
                row = cur.fetchone()
                if row is None:
                    return False
                rid = row["id"]
                roi = dict(row["data"])
                # §6.3：已锁 → 拒绝删除（annotation_locked 优先于 CAS）
                _check_geometry_lock(roi, True)
                _check_cas(roi, expected_revision)
                _append_history(roi)
                roi["deleted"] = True
                roi["deleted_at"] = time.time()
                roi["revision"] = int(roi.get("revision") or 1) + 1
                roi["change_seq"] = _bump_change_seq(
                    cur, roi.get("slide"), roi.get("token"),
                    roi.get("annotation_id"), "delete",
                    slide_id=roi.get("slide_id"))
                roi["updated_at"] = roi["deleted_at"]
                _update_roi_row(cur, rid, roi)
                return True
    finally:
        conn.close()


def restore_roi(annotation_id, expected_revision=None):
    """恢复 tombstone（按稳定 annotation_id）。成功返回 roi dict，否则 False。

    重做创建必须走这条路径：同一 client_action_id 不能再 INSERT（唯一索引
    覆盖 tombstone）。CAS 针对 tombstone 当前 revision。
    """
    if not annotation_id:
        raise ValueError("缺少 annotation_id")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, data FROM rois WHERE annotation_id=%s "
                    "FOR UPDATE",
                    (annotation_id,))
                row = cur.fetchone()
                if row is None:
                    return False
                rid = row["id"]
                roi = dict(row["data"])
                if not roi.get("deleted"):
                    # 已是活行：幂等返回
                    out = _roi_out(roi)
                    out["shared"] = _roi_shared_compat(roi)
                    return out
                _check_cas(roi, expected_revision)
                roi["deleted"] = False
                roi.pop("deleted_at", None)
                roi["revision"] = int(roi.get("revision") or 1) + 1
                roi["updated_at"] = time.time()
                roi["change_seq"] = _bump_change_seq(
                    cur, roi.get("slide"), roi.get("token") or "",
                    roi.get("annotation_id"), "restore",
                    slide_id=roi.get("slide_id"))
                _update_roi_row(cur, rid, roi)
                out = _roi_out(roi)
                out["shared"] = _roi_shared_compat(roi)
                return out
    finally:
        conn.close()


def update_roi_by_annotation_id(annotation_id, geom=None, note=None,
                                expected_revision=None):
    """按稳定 annotation_id 更新几何/备注（CAS 同 update_roi）。"""
    if not annotation_id:
        raise ValueError("缺少 annotation_id")
    if geom is not None and not isinstance(geom, dict):
        raise ValueError("geom 需为对象")
    note_clean = "_UNSET_"
    if note is not None:
        note_clean = _clean_note(note)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, data FROM rois WHERE annotation_id=%s "
                    "AND NOT deleted FOR UPDATE",
                    (annotation_id,))
                row = cur.fetchone()
                if row is None:
                    return False
                rid = row["id"]
                roi = dict(row["data"])
                token = roi.get("token") or ""
                # §6.3 几何锁检查先于 CAS（annotation_locked 优先）
                _check_geometry_lock(roi, geom is not None)
                _check_cas(roi, expected_revision)
                orig_type = roi.get("type", "rect")
                _append_history(roi)
                if geom is not None:
                    geom_full = dict(geom)
                    if orig_type == "rect" and "size_mm" not in geom_full:
                        geom_full["size_mm"] = roi.get("size_mm", 0.0)
                    if orig_type == "rect":
                        _effective_rect_geometry(roi, geom_full)
                        if ("side_px" in geom_full and "w" not in geom_full
                                and "h" not in geom_full
                                and roi.get("geometry_version") == 2):
                            side_val = geom_full.pop("side_px")
                            geom_full["w"] = side_val
                            geom_full["h"] = side_val
                    norm_g = _validate_geom(orig_type, geom_full)
                    norm_g["type"] = orig_type
                    roi.update(norm_g)
                    if orig_type == "rect" \
                            and norm_g.get("geometry_version") == 2 \
                            and "side_px" not in norm_g:
                        roi.pop("side_px", None)
                if note_clean != "_UNSET_":
                    roi["note"] = note_clean
                roi["revision"] = int(roi.get("revision") or 1) + 1
                roi["change_seq"] = _bump_change_seq(
                    cur, roi.get("slide"), token, roi.get("annotation_id"),
                    "update", slide_id=roi.get("slide_id"))
                roi["updated_at"] = time.time()
                _update_roi_row(cur, rid, roi)
                out = _roi_out(roi)
                out["shared"] = _roi_shared_compat(roi)
                return out
    finally:
        conn.close()


def set_roi_geometry_locked(annotation_id, geometry_locked,
                            expected_revision=None, actor_user_id=None,
                            actor=""):
    """切换标注几何位置锁（§6.3）：同一事务、行锁内 CAS + 状态翻转。

    - revision 递增（锁切换是一次可见状态变更，CAS 链保持单调）；
    - history 记录 op=lock/unlock + 操作者（actor；调用方传 "user:<uid>" /
      "visitor"——不含访客哈希，history 端点可直接返回）；
    - change_log 写 op=lock/unlock（change_seq 变化流可见）；
    - 目标不存在/已删除 → False；CAS 不符 → RevisionConflict；
      目标已是目标状态 → 幂等翻转（仍走完整流程，与 set_roi_shared 的
      显式写语义一致）。
    返回更新后的 roi dict（_roi_out 投影；index 由调用方按需补）。
    actor_user_id 仅为审计语义保留别名；不落 history。
    """
    if not annotation_id:
        raise ValueError("缺少 annotation_id")
    geometry_locked = bool(geometry_locked)
    op = "lock" if geometry_locked else "unlock"
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, data FROM rois WHERE annotation_id=%s "
                    "AND NOT deleted FOR UPDATE",
                    (annotation_id,))
                row = cur.fetchone()
                if row is None:
                    return False
                rid = row["id"]
                roi = dict(row["data"])
                _check_cas(roi, expected_revision)
                _append_history(roi, op=op, actor=actor or None)
                roi["geometry_locked"] = geometry_locked
                roi["revision"] = int(roi.get("revision") or 1) + 1
                roi["change_seq"] = _bump_change_seq(
                    cur, roi.get("slide"), roi.get("token") or "",
                    roi.get("annotation_id"), op,
                    slide_id=roi.get("slide_id"))
                roi["updated_at"] = time.time()
                _update_roi_row(cur, rid, roi)
                out = _roi_out(roi)
                out["shared"] = _roi_shared_compat(roi)
                return out
    finally:
        conn.close()


def upsert_ai_session_principal(session_id, user_id, slide=None, slide_id=None):
    """绑定 AI 会话属主（spots 读取主体）。已有会话不得改绑 user_id。

    P2（合同 §3.3/R-09）：slide_id + slide 快照双写；slide_id 显式给出时
    优先（名仅作快照/兼容）。读取路径（_internal_ai_read_subject）兼容双列。
    """
    if not session_id or not user_id:
        return False
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if slide_id is None and slide:
                    slide_id = _slide_id_of_name(cur, slide)
                cur.execute(
                    "INSERT INTO ai_session_principals "
                    "(session_id, user_id, slide, slide_id, updated_at) "
                    "VALUES (%s,%s,%s,%s,now()) "
                    "ON CONFLICT (session_id) DO UPDATE SET "
                    "slide=COALESCE(EXCLUDED.slide, ai_session_principals.slide), "
                    "slide_id=COALESCE(EXCLUDED.slide_id, "
                    "ai_session_principals.slide_id), "
                    "updated_at=now() "
                    "WHERE ai_session_principals.user_id = EXCLUDED.user_id "
                    "RETURNING user_id",
                    (session_id, user_id, slide or None, slide_id or None),
                )
                row = cur.fetchone()
                if row is not None:
                    return True
                cur.execute(
                    "SELECT user_id FROM ai_session_principals WHERE session_id=%s",
                    (session_id,))
                existing = cur.fetchone()
                return bool(existing and existing["user_id"] == user_id)
    finally:
        conn.close()


def get_ai_session_principal(session_id):
    """返回 {session_id, user_id, slide, slide_id} 或 None。"""
    if not session_id:
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT session_id, user_id, slide, slide_id "
                    "FROM ai_session_principals "
                    "WHERE session_id=%s",
                    (session_id,))
                row = cur.fetchone()
                return dict(row) if row else None
    finally:
        conn.close()


def list_changes(slide, after_seq, subject=None, access_context=None,
                 slide_id=None):
    """返回 change_seq > after_seq 的全部变更（含 tombstone）。

    Stage 3c-1：含评论增删（type=comment）与标注变更（type=annotation）；tombstone
    标注走 _roi_out 最小字段输出。
    0056 工单 A / P0：subject 非 None 时按主体过滤（annotation_access.
    filter_changes）——不可见标注的文本/几何/身份/tombstone 与挂靠评论一律
    不出流；被跳过事件的 seq 照常越过（游标推进语义不变）。
    P2（合同 §3.1/R-08）：``slide_id`` 给出时活动查询一律按 slide_id 过滤
    （rois/comments/annotation_access_events 三处）；slide_id IS NULL 的历史
    行 = unresolved，不展示在新资产下。仅给名（legacy 兼容）时按名查询。
    """
    if not isinstance(after_seq, (int, float)):
        after_seq = 0
    if slide_id is None and slide:
        # 名入参（未解析的调用方）：按冻结别名解析；解析不到 → unresolved 空集
        conn0 = _connect()
        try:
            with pg_store.transaction(conn0) as c0:
                with c0.cursor() as cur0:
                    slide_id = _slide_id_of_name(cur0, slide)
        finally:
            conn0.close()
    if slide_id is not None:
        roi_cond, cmt_cond, acc_cond = "slide_id=%s", "slide_id=%s", \
            "slide_id=%s AND seq > %s"
        roi_params, cmt_params, acc_params = (slide_id,), (slide_id,), \
            (slide_id, after_seq)
    else:
        roi_cond, cmt_cond, acc_cond = "slide=%s", "slide=%s", \
            "slide=%s AND seq > %s"
        roi_params, cmt_params, acc_params = (slide,), (slide,), \
            (slide, after_seq)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data FROM rois WHERE " + roi_cond +
                    " ORDER BY insert_seq", roi_params)
                rows = cur.fetchall()
                cur.execute(
                    "SELECT data FROM comments WHERE " + cmt_cond +
                    " ORDER BY created_at", cmt_params)
                crows = cur.fetchall()
                cur.execute(
                    "SELECT seq, slide, slide_id, annotation_id, op, "
                    "grantee_kind, grantee_id "
                    "FROM annotation_access_events WHERE " + acc_cond +
                    " ORDER BY seq", acc_params)
                access_rows = cur.fetchall()
        if subject is not None:
            ctx = (access_context if access_context is not None
                   else annotation_access.access_context_for(subject))
            parent_by_aid = {r["data"].get("annotation_id"): r["data"]
                             for r in rows}
            visible_raw = annotation_access.filter_rois(
                subject, [r["data"] for r in rows], ctx)
            visible_ids = {r.get("annotation_id") for r in visible_raw}
            rows = [r for r in rows
                    if r["data"].get("annotation_id") in visible_ids]
            crows = [r for r in crows
                     if annotation_access.can_read_annotation(
                         subject,
                         parent_by_aid.get(r["data"].get("annotation_id")),
                         ctx)]
        out = []
        for row in rows:
            r = row["data"]
            cs = r.get("change_seq")
            if cs is None or not isinstance(cs, (int, float)) or cs <= after_seq:
                continue
            rr = _roi_out(r)
            rr.setdefault("type", "annotation")
            if subject is not None:
                rr = annotation_access.public_roi_view(rr, subject)
                rr.setdefault("type", "annotation")
            out.append(rr)
        for row in crows:
            c = row["data"]
            cs = c.get("change_seq")
            if cs is None or not isinstance(cs, (int, float)) or cs <= after_seq:
                continue
            cc = dict(c)
            cc["type"] = "comment"
            if subject is not None:
                cc = annotation_access.public_comment_view(cc)
                cc["type"] = "comment"
            out.append(cc)
        for row in access_rows:
            ev = {
                "type": "access",
                "op": row["op"],
                "annotation_id": row["annotation_id"],
                "slide": row["slide"],
                "slide_id": row["slide_id"],
                "change_seq": int(row["seq"]),
                "grantee_kind": row["grantee_kind"],
                "grantee_id": row["grantee_id"],
                "reset_required": row["op"] == "revoke",
            }
            if subject is None or annotation_access.can_see_access_event(subject, ev):
                out.append(annotation_access.public_access_event_view(ev)
                           if subject is not None else ev)
        out.sort(key=lambda x: x.get("change_seq", 0))
        return out
    finally:
        conn.close()


def current_change_seq(slide, slide_id=None):
    """返回某切片当前的全局 change_seq 水位（无则 0）。

    P2：slide_id 给出时按 ID 查（活动查询口径）；仅名时先解析，解析不到
    按名查（legacy 兼容）。
    """
    cond, params = "slide=%s", (slide,)
    if slide_id is not None:
        cond, params = "slide_id=%s", (slide_id,)
    elif slide:
        conn0 = _connect()
        try:
            with pg_store.transaction(conn0) as c0:
                with c0.cursor() as cur0:
                    sid = _slide_id_of_name(cur0, slide)
            if sid is not None:
                cond, params = "slide_id=%s", (sid,)
        finally:
            conn0.close()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(MAX(seq), 0)::int AS s FROM change_log "
                    "WHERE " + cond, params)
                return int(cur.fetchone()["s"])
    finally:
        conn.close()


def set_roi_shared(token, index, shared, expected_revision=None,
                   grantee_kind=None, grantee_id=None, can_edit=False,
                   actor_user_id=None):
    """设置该 token 下第 index 条 ROI 的 shared 字段（跳过 tombstone）。

    expected_revision（CAS）：提供且与当前 revision 不符 → 抛 RevisionConflict。

    0056 新语义（工单 A / P0）：``shared`` **不再**是「对同片所有分享/用户
    公开」的全局开关，而是收窄为标注级授权的便捷封装：
      - shared=True + 显式 (grantee_kind, grantee_id)：授予该 user/share_token
        （can_edit 缺省只读）；
      - shared=True 无显式目标：仅当本条 token 是**真实分享链接**时，授予
        该 token 只读（「分享到本链接」）；token=admin 时**不再全局公开**
        （shared 标志照记，但不会让任何其他主体可见——旧行为的 breaking
        change，见 0056 迁移注释；要公开请显式授权）；
      - shared=False：撤销上述口径对应的授权（显式目标或本 token）。

    返回 True（沿用既有 bool 契约；授权明细经 list_grants 另查）。
    """
    shared_b = bool(shared)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                same = _fetch_live_rois_locked(cur, token)
                if index < 0 or index >= len(same):
                    return False
                rid = same[index]["id"]
                roi = dict(same[index]["data"])
                _check_cas(roi, expected_revision)
                roi["shared"] = shared_b
                _update_roi_row(cur, rid, roi)
                aid = roi.get("annotation_id")
                # —— 授权维护（同事务；aid 缺失的旧行不授权，仅记标志）——
                if aid:
                    if grantee_kind is not None or grantee_id is not None:
                        if grantee_kind not in ("user", "share_token"):
                            raise ValueError("grantee_kind 需为 user 或 share_token")
                        if not grantee_id:
                            raise ValueError("缺少 grantee_id")
                        if shared_b:
                            _grant_annotation_tx(
                                cur, aid, grantee_kind, grantee_id,
                                can_edit=bool(can_edit),
                                created_by=actor_user_id)
                        else:
                            _revoke_annotation_grant_tx(
                                cur, aid, grantee_kind, grantee_id)
                    elif shared_b and token != ADMIN_TOKEN:
                        # 「分享到本链接」：token 是真实分享 → 授予该 token 只读
                        _grant_annotation_tx(
                            cur, aid, "share_token", token, can_edit=False,
                            created_by=actor_user_id)
                    elif not shared_b and token != ADMIN_TOKEN:
                        _revoke_annotation_grant_tx(cur, aid, "share_token", token)
                return True
    finally:
        conn.close()


def set_roi_shared_by_annotation_id(annotation_id, shared,
                                    expected_revision=None, grantee_kind=None,
                                    grantee_id=None, can_edit=False,
                                    actor_user_id=None):
    """set_roi_shared 的 by-id 版（旧 index 路由写入绑定原语，2026-10-17）。

    与 set_roi_shared 语义逐项一致（含 0056 授权维护与「分享到本链接」，
    token 取该行自身 token），区别仅在定位：行锁内
    ``WHERE annotation_id=%s AND NOT deleted``——绝不按位置 index 二次定位
    （解析与写入之间列表漂移的 TOCTOU）。目标不存在/已删 → False；CAS 不符
    → RevisionConflict。返回 True（沿用 set_roi_shared 的 bool 契约；授权
    明细经 list_grants 另查）。
    """
    if not annotation_id:
        raise ValueError("缺少 annotation_id")
    shared_b = bool(shared)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, data FROM rois WHERE annotation_id=%s "
                    "AND NOT deleted FOR UPDATE",
                    (annotation_id,))
                row = cur.fetchone()
                if row is None:
                    return False
                rid = row["id"]
                roi = dict(row["data"])
                token = roi.get("token") or ""
                _check_cas(roi, expected_revision)
                roi["shared"] = shared_b
                _update_roi_row(cur, rid, roi)
                # —— 授权维护（同事务；aid 缺失的旧行不授权，仅记标志）——
                aid = roi.get("annotation_id")
                if aid:
                    if grantee_kind is not None or grantee_id is not None:
                        if grantee_kind not in ("user", "share_token"):
                            raise ValueError("grantee_kind 需为 user 或 share_token")
                        if not grantee_id:
                            raise ValueError("缺少 grantee_id")
                        if shared_b:
                            _grant_annotation_tx(
                                cur, aid, grantee_kind, grantee_id,
                                can_edit=bool(can_edit),
                                created_by=actor_user_id)
                        else:
                            _revoke_annotation_grant_tx(
                                cur, aid, grantee_kind, grantee_id)
                    elif shared_b and token != ADMIN_TOKEN:
                        # 「分享到本链接」：token 是真实分享 → 授予该 token 只读
                        _grant_annotation_tx(
                            cur, aid, "share_token", token, can_edit=False,
                            created_by=actor_user_id)
                    elif not shared_b and token != ADMIN_TOKEN:
                        _revoke_annotation_grant_tx(cur, aid, "share_token", token)
                return True
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 标注级授权（annotation_grants，0056 工单 A / P0）
#
# 跨主体可见性唯一的显式原语：grantee ∈ {user, share_token}；can_edit 缺省
# 只读。tombstone 不清理授权行（删除事件对被授权者仍需可见）。visibility_
# status 列随授权维护（private↔granted，仅报表/查询 aid）。
# --------------------------------------------------------------------------- #
def _record_access_event_tx(cur, annotation_id, op, grantee_kind, grantee_id,
                            actor_user_id=None):
    """写入 change_log + annotation_access_events（同 seq）。

    P2（合同 §3.1/R-14）：slide_id 随 roi 行当前值双写（离散列权威；历史
    NULL 保持 NULL = unresolved，不猜）。
    """
    cur.execute(
        "SELECT slide, token, slide_id FROM rois WHERE annotation_id=%s LIMIT 1",
        (annotation_id,))
    row = cur.fetchone()
    if row is None:
        return None
    slide, token = row["slide"], row["token"] or ""
    seq = _bump_change_seq(cur, slide, token, annotation_id, "access_" + op,
                           slide_id=row["slide_id"])
    cur.execute(
        "INSERT INTO annotation_access_events "
        "(seq, slide, annotation_id, op, grantee_kind, grantee_id, actor_user_id, "
        " slide_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (seq, slide, annotation_id, op, grantee_kind, grantee_id,
         actor_user_id or None, row["slide_id"]),
    )
    return seq


def _grant_annotation_tx(cur, annotation_id, grantee_kind, grantee_id,
                         can_edit=False, created_by=None):
    """同事务 UPSERT 一条授权（幂等；can_edit 以最新写入为准）。"""
    cur.execute(
        "INSERT INTO annotation_grants "
        "(annotation_id, grantee_kind, grantee_id, can_edit, created_by) "
        "VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (annotation_id, grantee_kind, grantee_id) "
        "DO UPDATE SET can_edit=EXCLUDED.can_edit, created_by=EXCLUDED.created_by",
        (annotation_id, grantee_kind, grantee_id, bool(can_edit),
         created_by or None),
    )
    cur.execute(
        "UPDATE rois SET visibility_status='granted' "
        "WHERE annotation_id=%s AND visibility_status='private'",
        (annotation_id,),
    )
    _record_access_event_tx(cur, annotation_id, "grant", grantee_kind,
                            grantee_id, actor_user_id=created_by)


def _revoke_annotation_grant_tx(cur, annotation_id, grantee_kind, grantee_id,
                                actor_user_id=None):
    """同事务删除一条授权；无剩余授权行时回落 private。"""
    cur.execute(
        "DELETE FROM annotation_grants "
        "WHERE annotation_id=%s AND grantee_kind=%s AND grantee_id=%s "
        "RETURNING 1",
        (annotation_id, grantee_kind, grantee_id),
    )
    existed = cur.fetchone() is not None
    cur.execute(
        "UPDATE rois SET visibility_status='private' "
        "WHERE annotation_id=%s AND visibility_status='granted' "
        "AND NOT EXISTS (SELECT 1 FROM annotation_grants g "
        "                WHERE g.annotation_id=%s)",
        (annotation_id, annotation_id),
    )
    if existed:
        _record_access_event_tx(cur, annotation_id, "revoke", grantee_kind,
                                grantee_id, actor_user_id=actor_user_id)
    return existed


def _list_annotation_grants_tx(cur, annotation_id):
    cur.execute(
        "SELECT annotation_id, grantee_kind, grantee_id, can_edit, created_by, "
        "extract(epoch from created_at)::float8 AS created_at "
        "FROM annotation_grants WHERE annotation_id=%s ORDER BY created_at",
        (annotation_id,))
    return [dict(r) for r in cur.fetchall()]


def grant_annotation_to_user(annotation_id, user_id, can_edit=False,
                             created_by=None):
    """授予某用户对标注的访问（缺省只读）。annotation_id 不存在 → ValueError。"""
    if not annotation_id or not user_id:
        raise ValueError("annotation_id 与 user_id 不能为空")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT 1 FROM rois WHERE annotation_id=%s",
                            (annotation_id,))
                if cur.fetchone() is None:
                    raise ValueError("标注不存在")
                _grant_annotation_tx(cur, annotation_id, "user", user_id,
                                     can_edit=bool(can_edit),
                                     created_by=created_by)
                return _list_annotation_grants_tx(cur, annotation_id)
    finally:
        conn.close()


def grant_annotation_to_share(annotation_id, share_token, can_edit=False,
                              created_by=None):
    """授予某分享链接对标注的访问（缺省只读）。token 需为真实分享。"""
    if not annotation_id or not share_token:
        raise ValueError("annotation_id 与 share_token 不能为空")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT 1 FROM rois WHERE annotation_id=%s",
                            (annotation_id,))
                if cur.fetchone() is None:
                    raise ValueError("标注不存在")
                share = _fetch_share(cur, share_token)
                if share is None:
                    raise ValueError("分享链接不存在")
                _grant_annotation_tx(cur, annotation_id, "share_token",
                                     share_token, can_edit=bool(can_edit),
                                     created_by=created_by)
                return _list_annotation_grants_tx(cur, annotation_id)
    finally:
        conn.close()


def revoke_grant(annotation_id, grantee_kind, grantee_id):
    """撤销一条授权（幂等；返回撤销前是否确有该行）。"""
    if grantee_kind not in ("user", "share_token"):
        raise ValueError("grantee_kind 需为 user 或 share_token")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _revoke_annotation_grant_tx(
                    cur, annotation_id, grantee_kind, grantee_id)
    finally:
        conn.close()


def list_grants(annotation_id=None, grantee_kind=None, grantee_id=None):
    """按条件列授权行（annotation_id / grantee 组合过滤；按创建时间升序）。"""
    clauses, params = [], []
    if annotation_id is not None:
        clauses.append("annotation_id=%s")
        params.append(annotation_id)
    if grantee_kind is not None:
        clauses.append("grantee_kind=%s")
        params.append(grantee_kind)
    if grantee_id is not None:
        clauses.append("grantee_id=%s")
        params.append(grantee_id)
    sql = ("SELECT annotation_id, grantee_kind, grantee_id, can_edit, "
           "created_by, extract(epoch from created_at)::float8 AS created_at "
           "FROM annotation_grants")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at"
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(sql, tuple(params))
                return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def annotation_grants_for_subject(grantee_kind, grantee_id):
    """主体的授权表 {annotation_id: can_edit}（annotation_access 过滤用）。"""
    if not grantee_kind or not grantee_id:
        return {}
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT annotation_id, can_edit FROM annotation_grants "
                    "WHERE grantee_kind=%s AND grantee_id=%s",
                    (grantee_kind, grantee_id))
                return {r["annotation_id"]: bool(r["can_edit"])
                        for r in cur.fetchall()}
    finally:
        conn.close()


def annotation_visibility_report():
    """0056 审计报表：owned / visitor_bound / shared_true_legacy / granted /
    unclaimed 计数与样本 annotation_id（前 20，认领核对用）。

    只读、不改动任何行（不批量公开/不推 owner/不删历史）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT annotation_id, owner_user_id IS NOT NULL "
                    "  AND coalesce(data->>'visitor','')='' AS owned, "
                    "  coalesce(data->>'visitor','')<>'' AS visitor_bound, "
                    "  shared AS shared_flag, visibility_status "
                    "FROM rois")
                rows = cur.fetchall()
        buckets = {
            "owned": [], "visitor_bound": [], "shared_true_legacy": [],
            "granted": [], "unclaimed": [],
        }
        for r in rows:
            aid = r["annotation_id"]
            if r["owned"]:
                buckets["owned"].append(aid)
            if r["visitor_bound"]:
                buckets["visitor_bound"].append(aid)
            if r["shared_flag"]:
                buckets["shared_true_legacy"].append(aid)
            if r["visibility_status"] == "granted":
                buckets["granted"].append(aid)
            if r["visibility_status"] == "unclaimed":
                buckets["unclaimed"].append(aid)
        return {k: {"count": len(v), "sample": v[:20]}
                for k, v in buckets.items()}
    finally:
        conn.close()


def review_roi(token, index, action):
    """Stage 3c-1：AI 标注审核（接受/驳回）。仅 source=ai 可审；否则 ValueError。

    成功返回更新后的 roi dict（含 index/review_status/revision）；token/index
    无效返回 False。bump revision + updated_at（不 bump change_seq）。
    """
    if action not in ("accept", "reject"):
        raise ValueError("action 需为 accept 或 reject")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                same = _fetch_live_rois_locked(cur, token)
                if index < 0 or index >= len(same):
                    return False
                rid = same[index]["id"]
                roi = dict(same[index]["data"])
                if roi.get("source") != "ai":
                    raise ValueError("仅 AI 标注可审核")
                roi["review_status"] = "accepted" if action == "accept" else "rejected"
                roi["revision"] = int(roi.get("revision") or 1) + 1
                roi["updated_at"] = time.time()
                _update_roi_row(cur, rid, roi)
                cur.execute(
                    "SELECT data FROM rois WHERE token=%s AND NOT deleted "
                    "ORDER BY insert_seq", (token,))
                all_rows = cur.fetchall()
                idx = next((i for i, row in enumerate(all_rows)
                            if row["data"].get("annotation_id") ==
                            roi.get("annotation_id")), 0)
                out = _roi_out(roi)
                out["index"] = idx
                out["shared"] = _roi_shared_compat(roi)
                return out
    finally:
        conn.close()


def review_roi_by_annotation_id(annotation_id, action):
    """按稳定 annotation_id 审核（review_roi 的 by-id 版，语义逐项一致）。

    仅 source=ai 可审；否则 ValueError。成功返回更新后的 roi dict（含
    review_status/revision，不含 index——id 寻址不需要）；id 无效或已删返回
    False。bump revision + updated_at（不 bump change_seq）。
    """
    if action not in ("accept", "reject"):
        raise ValueError("action 需为 accept 或 reject")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT id, data FROM rois "
                    "WHERE annotation_id=%s AND NOT deleted FOR UPDATE",
                    (annotation_id,))
                row = cur.fetchone()
                if row is None:
                    return False
                rid = row["id"]
                roi = dict(row["data"])
                if roi.get("source") != "ai":
                    raise ValueError("仅 AI 标注可审核")
                roi["review_status"] = "accepted" if action == "accept" else "rejected"
                roi["revision"] = int(roi.get("revision") or 1) + 1
                roi["updated_at"] = time.time()
                _update_roi_row(cur, rid, roi)
                out = _roi_out(roi)
                out["shared"] = _roi_shared_compat(roi)
                return out
    finally:
        conn.close()


def roi_count_by_token():
    """返回 {token: count} 计数表（跳过 tombstone）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT token, count(*) AS n FROM rois WHERE NOT deleted "
                    "GROUP BY token")
                rows = cur.fetchall()
        return {row["token"]: int(row["n"]) for row in rows}
    finally:
        conn.close()


def list_shared_rois_for_slides(slides, share_token=None):
    """返回授予 ``share_token`` 且 slide ∈ slides 的标注列表（跳过 tombstone）。

    0056 工单 A / P0：**必须**携带 share_token（None → ValueError，fail-closed
    ——绝不返回「同片全部 shared 标注」）。可见集合 = annotation_grants 中
    grantee=(share_token, token) 的授权行（管理员策展显式授予该链接的标注）。
    每项 index 沿用 get_roi/delete URL 的 token 内非 tombstone 位置口径。
    """
    if not share_token:
        raise ValueError("share_token is required（不再返回同片全部 shared 标注）")
    if not slides:
        return []
    slide_set = set(slides)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT r.data FROM rois r WHERE NOT deleted "
                    "AND r.annotation_id IN ("
                    "  SELECT annotation_id FROM annotation_grants "
                    "  WHERE grantee_kind='share_token' AND grantee_id=%s) "
                    "ORDER BY r.insert_seq", (share_token,))
                rows = cur.fetchall()
                # pre-filter index：各 token 非 tombstone 行内位置（get_roi 口径）
                cur.execute(
                    "SELECT token, annotation_id, "
                    "       (row_number() OVER (PARTITION BY token "
                    "         ORDER BY insert_seq) - 1)::int AS idx "
                    "FROM rois WHERE NOT deleted", ())
                idx_map = {(r["token"], r["annotation_id"]): r["idx"]
                           for r in cur.fetchall()}
        out = []
        for row in rows:
            r = row["data"]
            if r.get("slide") not in slide_set:
                continue
            rr = _geometry_lock_read(_rect_read_compat(dict(r)))
            rr["index"] = idx_map.get((r.get("token"), r.get("annotation_id")), 0)
            rr["shared"] = True
            rr.setdefault("type", "rect")
            rr["note"] = r.get("note", "")
            out.append(rr)
        out.sort(key=lambda x: x.get("ts", 0), reverse=True)
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 样本元数据 + 稳定 slide 身份（slides 表，name=legacy_filename）
# --------------------------------------------------------------------------- #
def _new_slide_id() -> str:
    return "sld_" + secrets.token_urlsafe(9)  # 12 位 urlsafe


def _slide_id_of_name(cur, name):
    """legacy 名 → slide_id（P2 关系双写解析；无行返回 None = unresolved）。

    只查 slides.legacy_filename 冻结映射（R-01），不建行、不猜；tombstone
    行的映射也算（关系快照指向原资产生代，读取门禁按 asset_state 拒绝）。
    """
    if not isinstance(name, str) or not name:
        return None
    cur.execute("SELECT slide_id FROM slides WHERE legacy_filename=%s",
                (name,))
    row = cur.fetchone()
    return row["slide_id"] if row is not None else None


# P1-B2（slide ID 化重构，docs/slide-id-refactor-p1-contract-20260925.md §8）：
# slides 行新增身份/状态列的写侧归一助手（与 slide_store.normalize_format_ext
# 同口径的小写白名单 ^[a-z0-9]{1,16}$；本模块不 import slide_store，保持
# share_store 依赖面不变）。
_SLIDE_FORMAT_EXT_RE = re.compile(r"^[a-z0-9]{1,16}$")


def _format_ext_from_name(name):
    """从 legacy 文件名后缀归一 format_ext；不匹配白名单返回 None。"""
    if not isinstance(name, str) or "." not in name:
        return None
    ext = name.rsplit(".", 1)[-1].strip().lower().lstrip(".")
    return ext if _SLIDE_FORMAT_EXT_RE.match(ext) else None


def _lazy_slide_columns(alias, name):
    """懒建行的 P1-B2 新列值（合同 §3.1 / 任务书 P1-B2 A.1）。

    legacy writer（V1/V2/COS/转换/导入等）同步完成即发布：asset_state='ready'
    （保持旧「写完即可读」行为——读取门禁只认 ready）、storage_layout='legacy'、
    original_filename=name、display_name（非空 alias 优先，否则 name）、
    format_ext（白名单后缀归一，不匹配 NULL）。
    """
    a = alias.strip() if isinstance(alias, str) else ""
    return {
        "display_name": a or name,
        "format_ext": _format_ext_from_name(name),
    }


def set_slide_meta(name, alias=None, note=None, owner_user_id=None, public=None,
                   requester_role=None):
    """设置/更新某切片的别名与备注；首次出现 name 时生成稳定 slide_id。

    语义与 json 一致，额外：name（legacy_filename）首次出现 → 新建 slides 行并
    生成稳定 slide_id；同名已存在 → 仅更新 alias/note/owner/public，slide_id 不动。

    P1-B2（合同 §8 兼容与退出条件）：按名懒建行是**迁移兼容层**——仅限既有
    legacy writer（上传 commit/归属校正/恢复/demo 目录/导入脚本）使用；正常
    新读写自 P3 起改走 slide_store 的 ID 原语。新行进入 asset_state='ready' /
    storage_layout='legacy'（_lazy_slide_columns），保持「写完即可读」旧行为。
    P2（合同 §7 / R-02 收口）：**alias 列停写**——UPDATE 不再 SET alias、
    INSERT 时 alias 列留 ''；``alias`` 入参仅映射 display_name（旧客户端
    兼容），读侧出参一律从 display_name 派生。
    """
    _reject_guest_write(requester_role)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT slide_id, owner_user_id FROM slides "
                            "WHERE legacy_filename=%s", (name,))
                row = cur.fetchone()
                if row is None:
                    slide_id = _new_slide_id()
                    lazy = _lazy_slide_columns(alias, name)
                    cur.execute(
                        "INSERT INTO slides (slide_id, legacy_filename, "
                        "original_filename, display_name, format_ext, "
                        "asset_state, storage_layout, published_at) "
                        "VALUES (%s,%s,%s,%s,%s,'ready','legacy',now())",
                        (slide_id, name, name, lazy["display_name"],
                         lazy["format_ext"]))
                    cur_owner = None
                else:
                    slide_id = row["slide_id"]
                    cur_owner = row["owner_user_id"]
                sets = []
                params = []
                if alias is not None:
                    a = alias.strip() if isinstance(alias, str) else ""
                    # R-02 收口（P2）：alias 列停写——入参仅映射 display_name
                    # （非空 → display_name=alias；清空 → 回落
                    # original_filename/legacy_filename，与回填规则同口径）。
                    sets.append(
                        "display_name=COALESCE(NULLIF(%s,''), original_filename,"
                        " legacy_filename, '')")
                    params.append(a)
                if note is not None:
                    n = note.strip() if isinstance(note, str) else ""
                    sets.append("note=%s")
                    params.append(n)
                if public is not None:
                    sets.append("public=%s")
                    params.append(bool(public))
                if cur_owner is None:
                    sets.append("owner_user_id=%s")
                    params.append(owner_user_id or _OWNER_USER_ID or None)
                if sets:
                    sets.append("updated_at=now()")
                    params.append(slide_id)
                    cur.execute(
                        "UPDATE slides SET " + ", ".join(sets) +
                        " WHERE slide_id=%s", params)
                # P4-app（合同 §7 强制收口）：deleted/deleting→ready 的同名
                # 复活分支**拆除**——最后一条 legacy writer（上传/ZIP/转换/
                # 恢复补归属）已全部切 slide_store 原语；tombstone 保持死亡
                # （删除后重传=新 slide_id 新资产，旧分享/授权/标注不继承）。
                # staging/legacy/failed 亦不复活（legacy 只经回填脚本验证，
                # 合同 §4）。
                cur.execute(
                    "SELECT display_name, note, owner_user_id, public "
                    "FROM slides WHERE slide_id=%s", (slide_id,))
                r2 = cur.fetchone()
                return {
                    # R-02：alias 出参从 display_name 派生（列值不再读）
                    "alias": (r2["display_name"] or ""),
                    "note": r2["note"] or "",
                    "owner_user_id": r2["owner_user_id"],
                    "public": bool(r2["public"]),
                }
    finally:
        conn.close()


def get_slide_meta(name):
    """返回某切片的 {alias, note}（无则空 dict，保证字段存在为空串）。

    P2（R-02 收口）：alias 出参从 display_name 派生（alias 列停写后不再读）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT display_name, note FROM slides "
                    "WHERE legacy_filename=%s", (name,))
                row = cur.fetchone()
        if row is None:
            return {"alias": "", "note": ""}
        return {"alias": row["display_name"] or "", "note": row["note"] or ""}
    finally:
        conn.close()


def get_slide_meta_full(name):
    """返回某切片的完整 meta（含 owner_user_id / public）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT display_name, note, owner_user_id, public "
                    "FROM slides WHERE legacy_filename=%s", (name,))
                row = cur.fetchone()
        if row is None:
            return {"alias": "", "note": "", "owner_user_id": None, "public": False}
        return {
            "alias": row["display_name"] or "",
            "note": row["note"] or "",
            "owner_user_id": row["owner_user_id"],
            "public": bool(row["public"]),
        }
    finally:
        conn.close()


def get_all_slide_meta_full():
    """返回全量 {name: {alias, note, owner_user_id, public}}。

    P2（R-02 收口）：alias 出参从 display_name 派生。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT legacy_filename, display_name, note, owner_user_id, "
                    "public FROM slides WHERE legacy_filename IS NOT NULL "
                    "ORDER BY legacy_filename")
                rows = cur.fetchall()
        out = {}
        for row in rows:
            out[row["legacy_filename"]] = {
                "alias": row["display_name"] or "",
                "note": row["note"] or "",
                "owner_user_id": row["owner_user_id"],
                "public": bool(row["public"]),
            }
        return out
    finally:
        conn.close()


def get_all_slide_meta():
    """返回全量 {name: {alias, note}}（R-02：alias 从 display_name 派生）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT legacy_filename, display_name, note FROM slides "
                    "WHERE legacy_filename IS NOT NULL ORDER BY legacy_filename")
                rows = cur.fetchall()
        return {row["legacy_filename"]: {"alias": row["display_name"] or "",
                                          "note": row["note"] or ""}
                for row in rows}
    finally:
        conn.close()


def get_slide_id(name):
    """返回某 legacy_filename 对应的稳定 slide_id；无则 None。

    P1-B2 起为**迁移兼容层**专用（R-01 / 计划 §5 限制项）：仅供既有按名的
    写通道残留（删除联动/admin 授权绑定等）与迁移脚本使用；正常读路径走
    slide_store.resolve_legacy_alias（带完整 descriptor + 状态门禁），新写
    路径自 P3 起走 slide_store 的 ID 原语。签名不变（旧调用方不动）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT slide_id FROM slides WHERE legacy_filename=%s",
                            (name,))
                row = cur.fetchone()
        return row["slide_id"] if row else None
    finally:
        conn.close()


def resolve_slide_ref(ref):
    """把 name（或已是稳定 id 的 slide_id）解析为稳定 slide_id；无则 None。

    P1-B2 起为**迁移兼容层**专用（R-01 / 计划 §5 限制项）：``sld_`` 前缀
    直返**不做存在性/状态检查**（当前无生产调用方，仅导出+测试保留）；正常
    解析一律走 slide_store.resolve_slide_id / resolve_legacy_alias（校验
    存在性并进入 authorize_read 状态门禁）。签名不变（旧调用方不动）。
    """
    if not ref:
        return None
    if isinstance(ref, str) and ref.startswith("sld_"):
        return ref
    return get_slide_id(ref)


def record_slide_asset(slide_id, legacy_revision):
    """记录切片内容资产 revision。返回 asset_id。

    legacy_revision 封装旧 mtime:size 指纹；content_sha256 由 3b-3 迁移工具填。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                asset_id = "ast_" + secrets.token_urlsafe(9)
                cur.execute(
                    "INSERT INTO slide_assets (asset_id, slide_id, legacy_revision) "
                    "VALUES (%s,%s,%s) RETURNING asset_id",
                    (asset_id, slide_id, legacy_revision))
                return cur.fetchone()["asset_id"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 项目（projects）
# --------------------------------------------------------------------------- #
def _dedupe(slides):
    seen = set()
    out = []
    for s in slides or []:
        if isinstance(s, str) and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _project_slide_rows(cur, slides, slide_ids=None):
    """project_slides 双写行构造（P2 / 合同 §3.2 / R-07）。

    slide_ids 显式给出（新客户端）时以 ID 为权威（名快照缺失时回查
    legacy_filename）；否则按名解析 slide_id（解析不到保持 NULL——与既有
    无行夹具兼容，唯一键 (project_id, slide_id) 对 NULL 不生效）。去重保序：
    slide_id 已见的行跳过（同名不同 ID 可并存）。
    P3（合同 §3.4）：slide_ids-only 关联（名数组为空）合法——ids 长于
    slides 时按 None 补齐（id_bundle 资产无名快照，行内 slide=""）。
    """
    rows = []
    seen_ids, seen_names = set(), set()
    slides = list(slides or [])
    ids = list(slide_ids) if slide_ids else [None] * len(slides)
    if slide_ids and len(slides) < len(ids):
        slides = slides + [None] * (len(ids) - len(slides))
    for name, sid_in in zip(slides, ids):
        sid = sid_in or None
        if sid is None and name:
            sid = _slide_id_of_name(cur, name)
        elif sid is not None and not name:
            cur.execute(
                "SELECT legacy_filename FROM slides WHERE slide_id=%s", (sid,))
            row = cur.fetchone()
            name = (row["legacy_filename"] or "") if row is not None else ""
        if sid is not None:
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
        elif name and name in seen_names:
            continue
        if name:
            seen_names.add(name)
        rows.append((name or "", sid))
    return rows


def create_project(name, note="", slides=None, owner_user_id=None,
                   requester_role=None, slide_ids=None, parent_project_id=None):
    """创建项目。pid=secrets.token_urlsafe(10)。返回新建项目 dict（含 pid）。

    P2（合同 §3.2/R-07）：slide_ids 优先或名数组（alias 解析）；project_slides
    写 slide_id + 文本快照双列；唯一键 (project_id, slide_id)——同名不同 ID
    可并存（0067 部分唯一索引）。
    2026-10-08 §5.3：可选 ``parent_project_id`` 建子文件夹——事务内锁 owner
    项目行并校验（存在/同 owner/未归档/层级 ≤5），非法抛
    :class:`ProjectParentError`（路由层映射 400/403/409）。
    """
    _reject_guest_write(requester_role)
    pid = "prj_" + secrets.token_urlsafe(10)
    now = time.time()
    uniq = _dedupe(slides)
    proj = {
        "name": str(name or "").strip() or "未命名项目",
        "note": str(note or ""),
        "slides": uniq,
        "created_at": now,
        "owner_user_id": owner_user_id or _OWNER_USER_ID or None,
        "archived": False,  # Stage 3c-2：归档纯只读开关，默认未归档
        "parent_project_id": parent_project_id or None,
    }
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if parent_project_id:
                    _validate_parent_tx(cur, proj["owner_user_id"],
                                        parent_project_id)
                cur.execute(
                    "INSERT INTO projects (project_id, name, note, "
                    "owner_user_id, created_at, parent_project_id) "
                    "VALUES (%s,%s,%s,%s, to_timestamp(%s), %s)",
                    (pid, proj["name"], proj["note"], proj["owner_user_id"],
                     now, proj["parent_project_id"]))
                rows = _project_slide_rows(cur, list(slides or []),
                                          slide_ids)
                for i, (s, sid) in enumerate(rows):
                    cur.execute(
                        "INSERT INTO project_slides "
                        "(project_id, slide, position, slide_id) "
                        "VALUES (%s,%s,%s,%s)", (pid, s, i, sid))
        out = dict(proj)
        out["pid"] = pid
        out["slides"] = [r[0] for r in rows if r[0]]
        out["slide_ids"] = [r[1] for r in rows if r[1]]
        return out
    finally:
        conn.close()


_PROJ_SEL = (
    "project_id, name, note, owner_user_id, archived, parent_project_id, "
    "extract(epoch from created_at)::float8 AS created_at"
)


# --------------------------------------------------------------------------- #
# 文件夹层级（2026-10-08 docs/admin-viewer-simplified-20261008.md §5.3）
#
# 文件夹 = 现有项目（project_id/成员/分享/归档语义不变），UI 文案叫「文件夹」。
# parent_project_id 校验：父项目存在且同 owner、未归档、不能是自己或自己的
# 子孙、层级不超过 5；移动/创建在事务内先 FOR UPDATE 锁住该 owner 的全部
# 项目行再做环检测（并发移动串行化，owner 内不可能成环）。
# --------------------------------------------------------------------------- #
#: 文件夹最大层级（根=1；层级不超过 5）
PROJECT_MAX_DEPTH = 5

#: update_project 的 parent_project_id 「不修改」哨兵（None = 移到根）
PROJECT_PARENT_UNCHANGED = object()


class ProjectParentError(Exception):
    """文件夹层级校验失败（§5.3）。status ∈ {400, 403, 409}；code/message
    供路由层映射稳定错误信封。"""

    def __init__(self, status, code, message):
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        super().__init__(message)


def _lock_owner_projects_tx(cur, owner_user_id):
    """锁住该 owner 的全部项目行（FOR UPDATE，事务内），返回
    {project_id: row}。并发移动/创建同一 owner 的文件夹在此串行化。"""
    cur.execute(
        "SELECT project_id, parent_project_id, archived, owner_user_id "
        "FROM projects WHERE owner_user_id=%s ORDER BY project_id FOR UPDATE",
        (owner_user_id,))
    return {r["project_id"]: dict(r) for r in cur.fetchall()}


def _lookup_parent_global(cur, parent_project_id):
    """父项目的全库快照（区分 404/403 用；权威校验在 owner 行锁之后）。"""
    cur.execute(
        "SELECT project_id, parent_project_id, archived, owner_user_id "
        "FROM projects WHERE project_id=%s", (parent_project_id,))
    row = cur.fetchone()
    return dict(row) if row is not None else None


def _chain_depth(rows_by_id, pid):
    """pid 的层级（根=1）：沿 parent 链向上计数；环（脏数据）按已访问截断。"""
    d = 0
    node = pid
    seen = set()
    while node is not None and node not in seen:
        seen.add(node)
        d += 1
        row = rows_by_id.get(node)
        node = row["parent_project_id"] if row else None
    return d


def _descendant_max_relative_depth(rows_by_id, pid):
    """pid 子树内相对 pid 的最大深度（pid 自身=0）。"""
    children = {}
    for row in rows_by_id.values():
        parent = row["parent_project_id"]
        if parent:
            children.setdefault(parent, []).append(row["project_id"])
    best = 0
    stack = [(pid, 0)]
    seen = set()
    while stack:
        node, rel = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        best = max(best, rel)
        for child in children.get(node, []):
            stack.append((child, rel + 1))
    return best


def _validate_parent_tx(cur, owner_user_id, parent_project_id, *,
                        self_id=None):
    """事务内校验父项目并锁 owner 行（§5.3）。

    返回锁定快照 rows_by_id（移动路径复用做环/层级检测）。非法抛
    :class:`ProjectParentError`：
      - 父不存在 → 400 parent_not_found；
      - 父属其他 owner → 403 parent_not_owner；
      - 父已归档 → 409 parent_archived；
      - 自引用 / 移入自己的子孙 → 409 parent_cycle；
      - 超过 5 层 → 409 parent_depth_exceeded。
    """
    parent = _lookup_parent_global(cur, parent_project_id)
    if parent is None:
        raise ProjectParentError(400, "parent_not_found", "父文件夹不存在")
    if (parent["owner_user_id"] or "") != (owner_user_id or ""):
        raise ProjectParentError(
            403, "parent_not_owner", "不能把其他用户的文件夹作为父级")
    rows_by_id = _lock_owner_projects_tx(cur, owner_user_id)
    parent_locked = rows_by_id.get(parent_project_id)
    if parent_locked is None:
        # 锁窗口内被删除：按不存在处理
        raise ProjectParentError(400, "parent_not_found", "父文件夹不存在")
    if parent_locked["archived"]:
        raise ProjectParentError(
            409, "parent_archived", "父文件夹已归档，不能放入子文件夹")
    if self_id is not None:
        if parent_project_id == self_id:
            raise ProjectParentError(
                409, "parent_cycle", "不能把文件夹设为自己的父级")
        node = parent_project_id
        seen = set()
        while node is not None and node not in seen:
            if node == self_id:
                raise ProjectParentError(
                    409, "parent_cycle", "不能把文件夹移动到自己的子文件夹内")
            seen.add(node)
            row = rows_by_id.get(node)
            node = row["parent_project_id"] if row else None
        # 层级：新自身层级 = 父层级 + 1；子树整体随移动平移，最深 descendant
        # 不得超过 5
        new_self_depth = _chain_depth(rows_by_id, parent_project_id) + 1
        rel_max = _descendant_max_relative_depth(rows_by_id, self_id)
        if new_self_depth + rel_max > PROJECT_MAX_DEPTH:
            raise ProjectParentError(
                409, "parent_depth_exceeded",
                "文件夹层级超过 %d 层上限" % PROJECT_MAX_DEPTH)
    else:
        # 新建子文件夹：父层级 + 1 ≤ 5
        if _chain_depth(rows_by_id, parent_project_id) + 1 > PROJECT_MAX_DEPTH:
            raise ProjectParentError(
                409, "parent_depth_exceeded",
                "文件夹层级超过 %d 层上限" % PROJECT_MAX_DEPTH)
    return rows_by_id


# 用户删除（deleting/deleted）的资产不属于项目的活动视图：列表、计数与
# 选择都不再出现它。project_slides 的成员行与切片墓碑原样保留（历史/审计），
# 只是不投影。missing/failed 等其它状态照常列出（由前端如实显示不可读）。
_PROJECT_HIDDEN_ASSET_STATES = ("deleting", "deleted")


def _fetch_project(cur, pid):
    cur.execute("SELECT " + _PROJ_SEL + " FROM projects WHERE project_id=%s", (pid,))
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute("SELECT ps.slide, ps.slide_id FROM project_slides ps "
                "LEFT JOIN slides s ON s.slide_id = ps.slide_id "
                "WHERE ps.project_id=%s "
                "AND (s.asset_state IS NULL OR NOT (s.asset_state = ANY(%s))) "
                "ORDER BY ps.position",
                (pid, list(_PROJECT_HIDDEN_ASSET_STATES)))
    prows = cur.fetchall()
    d = dict(row)
    d["pid"] = pid
    d["slides"] = [r["slide"] for r in prows]
    d["slide_ids"] = [r["slide_id"] for r in prows if r["slide_id"]]
    # Row-aligned pairs: slide_ids drops rows without an ID, so it cannot be zipped with
    # slides. id_bundle assets have no unique name; clients must address them by slide_id.
    d["slide_refs"] = [{"slide": r["slide"], "slide_id": r["slide_id"]} for r in prows]
    return d


def list_projects():
    """返回全部项目列表，每项附加 pid、slide_count；按 created_at 倒序。"""
    conn = _connect()
    try:
        items = []
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT " + _PROJ_SEL +
                            " FROM projects ORDER BY created_at DESC, project_id")
                rows = cur.fetchall()
                for row in rows:
                    d = _fetch_project(cur, row["project_id"])
                    if d is not None:
                        d["slide_count"] = len(d["slides"])
                        items.append(d)
        return items
    finally:
        conn.close()


def get_project(pid):
    """返回单个项目 dict（附加 pid）；不存在返回 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _fetch_project(cur, pid)
    finally:
        conn.close()


def update_project(pid, *, name=None, note=None, slides=None, slide_ids=None,
                   parent_project_id=PROJECT_PARENT_UNCHANGED):
    """更新项目字段（仅更新非 None 字段）。返回更新后的 dict；不存在返回 None。

    2026-10-08 §5.3：``parent_project_id`` 支持「移动文件夹」——
    PROJECT_PARENT_UNCHANGED（缺省）= 不动；None = 移到根；str = 移到该父
    （事务内锁 owner 行 + 环/层级/同 owner/归档校验，非法抛
    :class:`ProjectParentError`）。
    """
    move_parent = parent_project_id is not PROJECT_PARENT_UNCHANGED
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT name, owner_user_id FROM projects "
                            "WHERE project_id=%s", (pid,))
                prow = cur.fetchone()
                if prow is None:
                    return None
                if move_parent:
                    if parent_project_id:
                        _validate_parent_tx(
                            cur, prow["owner_user_id"], parent_project_id,
                            self_id=pid)
                        cur.execute(
                            "UPDATE projects SET parent_project_id=%s "
                            "WHERE project_id=%s",
                            (parent_project_id, pid))
                    else:
                        # null = 移到根
                        cur.execute(
                            "UPDATE projects SET parent_project_id=NULL "
                            "WHERE project_id=%s", (pid,))
                if name is not None:
                    cur.execute("UPDATE projects SET name=%s WHERE project_id=%s",
                                (str(name).strip() or prow["name"] or "未命名项目",
                                 pid))
                if note is not None:
                    cur.execute("UPDATE projects SET note=%s WHERE project_id=%s",
                                (str(note), pid))
                if slides is not None:
                    rows = _project_slide_rows(cur, list(slides or []),
                                               slide_ids)
                    # 整表替换只针对活动视图：已删除资产的历史成员行不在
                    # 客户端看到的列表里，替换后按原相对顺序接在末尾保留。
                    cur.execute(
                        "SELECT ps.slide, ps.slide_id FROM project_slides ps "
                        "JOIN slides s ON s.slide_id = ps.slide_id "
                        "WHERE ps.project_id=%s AND s.asset_state = ANY(%s) "
                        "ORDER BY ps.position",
                        (pid, list(_PROJECT_HIDDEN_ASSET_STATES)))
                    new_ids = {sid for _s, sid in rows if sid}
                    rows = rows + [(r["slide"], r["slide_id"])
                                   for r in cur.fetchall()
                                   if r["slide_id"] not in new_ids]
                    cur.execute("DELETE FROM project_slides WHERE project_id=%s", (pid,))
                    for i, (s, sid) in enumerate(rows):
                        cur.execute(
                            "INSERT INTO project_slides "
                            "(project_id, slide, position, slide_id) "
                            "VALUES (%s,%s,%s,%s)", (pid, s, i, sid))
                return _fetch_project(cur, pid)
    finally:
        conn.close()


def _slide_owner_mismatch(cur, slide_id, name, proj_owner):
    """P0-W2（审计 P0-2）：项目成员切片归属复核。True = 不一致或不可判定。

    slide_id 优先直查，其次按名（legacy 冻结映射）；无行（历史名行/已删除
    资产）视为不可判定 → 拒绝（fail-closed；HTTP 层解析失败在先，仅显式传
    requester_role 的调用走到这里）。双方 owner 归一（'' → NULL）后比较。
    """
    if slide_id:
        cur.execute("SELECT owner_user_id FROM slides WHERE slide_id=%s",
                    (slide_id,))
    elif name:
        cur.execute("SELECT owner_user_id FROM slides WHERE legacy_filename=%s",
                    (name,))
    else:
        return True
    row = cur.fetchone()
    if row is None:
        return True
    return (row["owner_user_id"] or None) != (proj_owner or None)


def add_slides_to_project(pid, slides, slide_ids=None, requester_role=None):
    """向项目追加切片（去重保序）。返回更新后的 dict；不存在返回 None。

    P2（合同 §3.2）：slide_ids 优先或名数组；双列写入；同名不同 ID 可并存
    （去重键 = slide_id，NULL-ID 行按名去重保持旧行为）。
    P0-W2（审计 P0-2）：切片归属校验——成员切片 owner 须与项目 owner 一致
    （防他人切片被归档项目锁死），不一致 raise PermissionError（路由层映射
    403）。requester_role="owner"（== user_store.ROLE_OWNER；本模块不
    import user_store，与 _reject_guest_write 同口径用字面量）豁免——管理
    语义；requester_role=None = 内部调用（百度/转换/producer 导入链，调用
    前已自行复核 project.owner == 导入用户）不限制。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT owner_user_id FROM projects WHERE project_id=%s",
                    (pid,))
                prow = cur.fetchone()
                if prow is None:
                    return None
                proj_owner = prow["owner_user_id"] or None
                # None=内部调用（不限制）；"owner"=豁免；其余显式角色复核
                check_ownership = requester_role is not None \
                    and requester_role != "owner"
                cur.execute("SELECT slide, slide_id FROM project_slides "
                            "WHERE project_id=%s ORDER BY position", (pid,))
                prows = cur.fetchall()
                existing = [r["slide"] for r in prows]
                seen_ids = {r["slide_id"] for r in prows if r["slide_id"]}
                seen = set(existing)
                pos = len(existing)
                for s, sid in _project_slide_rows(cur, slides, slide_ids):
                    if check_ownership and _slide_owner_mismatch(
                            cur, sid, s, proj_owner):
                        raise PermissionError(
                            "切片 %s 归属与项目 owner 不一致，拒绝加入项目"
                            % (sid or s))
                    if sid is not None:
                        if sid in seen_ids:
                            continue
                        seen_ids.add(sid)
                    elif s and s in seen:
                        continue
                    if s:
                        seen.add(s)
                    cur.execute(
                        "INSERT INTO project_slides "
                        "(project_id, slide, position, slide_id) "
                        "VALUES (%s,%s,%s,%s)", (pid, s, pos, sid))
                    existing.append(s)
                    pos += 1
                return _fetch_project(cur, pid)
    finally:
        conn.close()


def remove_slide_from_project(pid, slide):
    """从项目移除某切片（legacy 名通道）。返回更新后的 dict；不存在或无该切片返回 None。

    P2：优先按解析到的 slide_id 删（同名不同 ID 时精确命中）；解析不到回退
    名删（无行兼容）。ID 通道请用 remove_slide_from_project_by_id。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT project_id FROM projects WHERE project_id=%s", (pid,))
                if cur.fetchone() is None:
                    return None
                sid = _slide_id_of_name(cur, slide)
                if sid is not None:
                    cur.execute(
                        "DELETE FROM project_slides "
                        "WHERE project_id=%s AND (slide_id=%s OR "
                        "(slide_id IS NULL AND slide=%s)) RETURNING 1",
                        (pid, sid, slide))
                else:
                    cur.execute(
                        "DELETE FROM project_slides WHERE project_id=%s AND slide=%s "
                        "RETURNING 1", (pid, slide))
                if cur.fetchone() is None:
                    return None
                return _fetch_project(cur, pid)
    finally:
        conn.close()


def remove_slide_from_project_by_id(pid, slide_id):
    """从项目按 slide_id 移除切片（P2 新端点 DELETE /api/project/<pid>/slides/
    <slide_id> 的存储原语）。返回更新后的 dict；不存在或无该切片返回 None。"""
    if not isinstance(slide_id, str) or not slide_id:
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT project_id FROM projects WHERE project_id=%s", (pid,))
                if cur.fetchone() is None:
                    return None
                cur.execute(
                    "DELETE FROM project_slides "
                    "WHERE project_id=%s AND slide_id=%s RETURNING 1",
                    (pid, slide_id))
                if cur.fetchone() is None:
                    return None
                return _fetch_project(cur, pid)
    finally:
        conn.close()


def delete_project(pid):
    """删除项目（仅删项目记录，不动切片文件）。返回是否删除成功。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("DELETE FROM projects WHERE project_id=%s RETURNING 1", (pid,))
                return cur.fetchone() is not None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 标注（annotations）汇总
# --------------------------------------------------------------------------- #
def annotations_by_slide(subject=None, access_context=None):
    """把 rois 按 slide 分组聚合（结构与 json 完全一致）。

    0056 工单 A / P0：subject 非 None 时**先按主体过滤再分组计数**（见
    annotation_access.can_read_annotation；HTTP 层一律传 subject，subject=None
    仅限管理清点/存量 store 级测试）。过滤不重排 index——index 是该 token
    全部非 tombstone 行内的 pre-filter 位置（与 get_roi/delete URL 口径一致），
    绝不是可见子集的 0..n 重编号；定位请优先用 annotation_id。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data, slide_id FROM rois WHERE NOT deleted "
                    "ORDER BY insert_seq")
                rows = cur.fetchall()
        ctx = (access_context if access_context is not None
               else annotation_access.access_context_for(subject)
               if subject is not None else None)
        from collections import defaultdict
        counters = defaultdict(int)
        by_slide = {}
        for row in rows:
            r = row["data"]
            tok = r.get("token")
            idx = counters[tok]
            counters[tok] += 1
            if subject is not None and not annotation_access.can_read_annotation(
                    subject, r, ctx):
                continue
            # P3（合同 §1.2；P2 偏差 #1 收口）：分组键切 **slide_id**——hotfix
            # 2026-10-19 起以离散列 ``rois.slide_id`` 为权威（列优先），JSON
            # data 的 slide_id 次之，NULL-ID 历史行按名称快照兜底。生产存在
            # 「列有 ID、JSON 缺键」的分裂态行：此前按 JSON 分组会把同片行拆进
            # ID 组与名组两个桶，/api/annotations 的「ID 组 or 名组」二选一
            # 取组导致「新增一条标注隐藏全部旧标注」。id_bundle 资产可同
            # original_filename，按名分组会串；ID 维度天然隔离。消费方（app
            # 的 /api/annotations 系）按 ID 取组、展示键在 DTO 层投影。
            slide = row["slide_id"] or r.get("slide_id") or r.get("slide")
            lbl = _norm_label(r.get("label"))
            grp_map = by_slide.setdefault(slide, {})
            grp = grp_map.get(lbl)
            if grp is None:
                grp = {"label": lbl, "count": 0, "items": []}
                grp_map[lbl] = grp
            grp["count"] += 1
            item = {
                "index": idx,
                "token": tok,
                "slide": r.get("slide"),
                # hotfix 2026-10-19：JSON 缺键时以列回填（_slide_id_read_compat
                # 同口径），items.slide_id 与分组键一致。
                "slide_id": r.get("slide_id") or row["slide_id"],
                "type": r.get("type", "rect"),
                "x": r.get("x"),
                "y": r.get("y"),
                "size_mm": r.get("size_mm"),
                "side_px": r.get("side_px"),
                "ts": r.get("ts"),
                "shared": _roi_shared_compat(r),
                "note": r.get("note", ""),
                "annotation_id": r.get("annotation_id"),
                "source": r.get("source", "human"),
                # 0056：作者口径与能力位判定所需（annotation_access）
                "owner_user_id": r.get("owner_user_id"),
                "created_by_session_id": r.get("created_by_session_id", ""),
                "change_seq": r.get("change_seq"),
                "revision": r.get("revision", 1),
                "review_status": r.get("review_status", "none"),
                "visitor": (r.get("visitor") or "")[:8],
                # §6.3：锁状态顶层布尔（旧行缺省 false）
                "geometry_locked": bool(r.get("geometry_locked")),
            }
            for k in ("w", "h", "geometry_version", "x1", "y1", "x2", "y2", "points"):
                if k in r:
                    item[k] = r[k]
            # 升级 C 读兼容：旧 rect 只有 side_px → 补 w/h（仅输出）
            if item["type"] == "rect":
                _rect_read_compat(item)
            grp["items"].append(item)
        result = {}
        for slide, grp_map in by_slide.items():
            result[slide] = list(grp_map.values())
        return result
    finally:
        conn.close()


def annotations_by_project(pid=None, subject=None, access_context=None):
    """与 annotations_by_slide 同结构，但可选按项目内的 slides 过滤（subject
    语义同 annotations_by_slide：非 None 时按主体过滤）。

    P2（合同 §3.2）：过滤按 project_slides.slide_id（权威）；P3 起分组键=
    slide_id（hotfix 2026-10-19 以 rois.slide_id 列为权威，NULL-ID 历史
    行按名快照分组），与 project_ids/project_slides 双集过滤天然对齐——
    项目内同名不同 ID 并存互不串。
    """
    by_slide = annotations_by_slide(subject=subject,
                                    access_context=access_context)
    if pid is None:
        return by_slide
    proj = get_project(pid)
    project_ids = set(proj.get("slide_ids", []) or []) if proj else set()
    project_slides = set(proj.get("slides", [])) if proj else set()
    return {
        slide: groups
        for slide, groups in by_slide.items()
        if slide in project_ids or slide in project_slides
    }


# --------------------------------------------------------------------------- #
# 评论线程（comments）—— Stage 3c-1（docs §5.3）
#
# comments 表存权威 dict 在 data JSONB（同 rois 语义），离散列镜像供过滤/索引。
# 增删 bump change_log（op=comment_add/comment_delete），list_changes 以 type=comment
# 返回。语义与 json 完全一致。
# --------------------------------------------------------------------------- #
def _insert_comment(cur, cmt: dict, cid: str):
    """插入一条 comment：data 存权威 dict，离散列镜像。

    P2（合同 §3.1/R-08）：slide_id（权威）+ slide（名称快照）双列。
    """
    now = cmt.get("updated_at") or cmt.get("created_at") or time.time()
    cur.execute(
        "INSERT INTO comments "
        "(comment_id, annotation_id, slide, token, author_user_id, author_label, "
        " body, parent_id, resolved, deleted, created_at, updated_at, data, "
        " slide_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, to_timestamp(%s), to_timestamp(%s), %s, %s)",
        (
            cid, cmt.get("annotation_id", ""), cmt.get("slide", ""),
            cmt.get("token", ""), cmt.get("author_user_id"),
            cmt.get("author_label", "访客"), cmt.get("body", ""),
            cmt.get("parent_id"), bool(cmt.get("resolved", False)),
            bool(cmt.get("deleted", False)), cmt.get("created_at", now), now,
            psycopg.types.json.Jsonb(cmt), cmt.get("slide_id") or None,
        ),
    )


def _update_comment_row(cur, cid: str, cmt: dict):
    """更新一条 comment（data + 离散镜像列）。"""
    now = cmt.get("updated_at") or time.time()
    cur.execute(
        "UPDATE comments SET resolved=%s, deleted=%s, updated_at=to_timestamp(%s), "
        "data=%s WHERE comment_id=%s",
        (bool(cmt.get("resolved", False)), bool(cmt.get("deleted", False)), now,
         psycopg.types.json.Jsonb(cmt), cid),
    )


def add_comment(annotation_id, slide, token, body, author_user_id=None,
                author_label="", parent_id=None, requester_role=None,
                slide_id=None):
    """新增评论；返回 comment dict（含 comment_id/change_seq）。语义同 json。

    P2（合同 §3.1/R-08）：slide_id 显式优先，否则按 legacy 名解析；解析不到
    保持 NULL = unresolved。comments/change_log 双写。
    """
    body_clean = _clean_comment_body(body)
    if not body_clean:
        raise ValueError("评论正文不能为空")
    now = time.time()
    cid = "cmt_" + uuid.uuid4().hex
    cmt = {
        "comment_id": cid,
        "annotation_id": annotation_id or "",
        "slide": slide or "",
        "token": token or "",
        "author_user_id": author_user_id or None,
        "author_label": (author_label or "").strip()[:80] or "访客",
        "body": body_clean,
        "parent_id": parent_id or None,
        "resolved": False,
        "deleted": False,
        "created_at": now,
        "updated_at": now,
    }
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                eff_slide_id = slide_id
                if eff_slide_id is None:
                    eff_slide_id = _slide_id_of_name(cur, slide)
                cmt["slide_id"] = eff_slide_id or None
                cmt["change_seq"] = _bump_change_seq(
                    cur, slide, token, cid, "comment_add",
                    slide_id=eff_slide_id)
                _insert_comment(cur, cmt, cid)
                return dict(cmt)
    finally:
        conn.close()


def list_comments(annotation_id=None, slide=None, subject=None,
                  access_context=None, access_token=None, project=True,
                  slide_id=None):
    """返回评论列表（跳过软删）。可按 annotation_id / slide 过滤。按 created_at 升序。

    0056：subject 非 None 时按父标注可见性过滤（父不可见 → 评论不出；
    父缺失/无 annotation_id 的挂靠按不可见处理，fail-closed）。
    """
    clauses = ["NOT deleted"]
    params = []
    if annotation_id is not None:
        clauses.append("annotation_id=%s")
        params.append(annotation_id)
    if slide_id is not None:
        # P2 活动查询口径：按 ID 过滤（NULL-ID 历史行 = unresolved 不出）
        clauses.append("slide_id=%s")
        params.append(slide_id)
    elif slide is not None:
        clauses.append("slide=%s")
        params.append(slide)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data FROM comments WHERE " + " AND ".join(clauses) +
                    " ORDER BY created_at", params)
                rows = cur.fetchall()
                if subject is not None:
                    cur.execute(
                        "SELECT data FROM rois WHERE NOT deleted")
                    parent_by_aid = {r["data"].get("annotation_id"): r["data"]
                                     for r in cur.fetchall()}
        out = [dict(r["data"]) for r in rows]
        if subject is not None:
            ctx = (access_context if access_context is not None
                   else annotation_access.access_context_for(subject))
            out = [cmt for cmt in out
                   if annotation_access.can_read_annotation(
                       subject, parent_by_aid.get(cmt.get("annotation_id")), ctx)]
        if not project:
            return out
        return [annotation_access.public_comment_view(cmt, access_token=access_token)
                for cmt in out]
    finally:
        conn.close()


def resolve_comment(comment_id, resolved=True):
    """设置评论 resolved 状态；返回是否成功。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data FROM comments WHERE comment_id=%s AND NOT deleted",
                    (comment_id,))
                row = cur.fetchone()
                if row is None:
                    return False
                cmt = dict(row["data"])
                cmt["resolved"] = bool(resolved)
                cmt["updated_at"] = time.time()
                _update_comment_row(cur, comment_id, cmt)
                return True
    finally:
        conn.close()


def delete_comment(comment_id):
    """软删评论（deleted=true + bump change_seq）；返回是否成功。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT data FROM comments WHERE comment_id=%s AND NOT deleted",
                    (comment_id,))
                row = cur.fetchone()
                if row is None:
                    return False
                cmt = dict(row["data"])
                cmt["deleted"] = True
                cmt["updated_at"] = time.time()
                cmt["change_seq"] = _bump_change_seq(
                    cur, cmt.get("slide"), cmt.get("token"), comment_id,
                    "comment_delete", slide_id=cmt.get("slide_id"))
                _update_comment_row(cur, comment_id, cmt)
                return True
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 审计日志（audit_events）—— Stage 3c-2（docs §5.3/§6.4）
#
# 语义与 json 完全一致：协作操作日志，best-effort（record_audit 内部吞掉写失败），
# 绝不写密钥/明文密码。pg 侧存 audit_events 表。
# --------------------------------------------------------------------------- #
AUDIT_MAX_EVENTS = 5000

# 审计写失败节流告警（进程内简单实现，风格同 app.py `_warn_secret_throttled` /
# site_stats_store `_warn_state`）：PG 故障期间每次审计写都失败，300s 至多记一条
# 堆栈，防止刷屏；复位函数供测试清零（惯例同 site_stats_store._reset_warn_state）。
_AUDIT_FAIL_LOG_INTERVAL_SECONDS = 300.0
_audit_fail_log_last = {"last": None}  # None = 从未记过（首条必发，不被节流）


def _reset_audit_fail_log_state():
    """测试辅助：清空写失败日志节流状态（下一条失败日志必然发出）。"""
    _audit_fail_log_last["last"] = None


def record_audit(action, actor_user_id=None, actor_role=None, target_type=None,
                 target_id=None, slide=None, detail=None, ts=None, slide_id=None):
    """best-effort 追加一条审计事件；写失败吞掉返回 False，绝不抛异常。

    与 json 的 record_audit 同签名同语义（dispatcher 在 dual 下同参重放到 pg，
    各自生成独立 event_id，跨库 id 无需一致）。detail 绝不存 api_key/明文密码。
    P2（合同 §3.4/R-14）：``slide_id`` 与名称快照 ``slide`` 双写。
    """
    ev_id = "aud_" + secrets.token_hex(16)
    detail = dict(detail) if isinstance(detail, dict) else {}
    try:
        conn = _connect()
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    cur.execute(
                        "INSERT INTO audit_events "
                        "(event_id, ts, actor_user_id, actor_role, action, "
                        " target_type, target_id, slide, detail, slide_id) "
                        "VALUES (%s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s)",
                        (ev_id, ts if ts is not None else time.time(),
                         actor_user_id or None, actor_role or "",
                         str(action or ""), target_type or None, target_id or None,
                         slide or None, psycopg.types.json.Jsonb(detail),
                         slide_id or None),
                    )
            return True
        finally:
            conn.close()
    except Exception:
        # best-effort 契约不变：吞异常、返回 False、绝不抛出（审计不能打挂
        # 业务路径）。但不再静默——节流记一条 exception 级日志（fix
        # 2026-09-11：此前完全吞掉，PG 故障期间审计 100% 丢失且零信号）。
        # 只记 action 名与 target_type/target_id 标识 + 异常堆栈；detail 可能
        # 含业务数据/密钥相邻信息，绝不入日志。
        now_mono = time.monotonic()
        last = _audit_fail_log_last["last"]
        if last is None or (now_mono - last) >= \
                _AUDIT_FAIL_LOG_INTERVAL_SECONDS:
            _audit_fail_log_last["last"] = now_mono
            _LOG.exception(
                "[audit] 审计写入失败（best-effort 返回 False；"
                "action=%s target_type=%s target_id=%s；节流窗口内同类仅记本条）",
                str(action or ""), target_type or None, target_id or None)
        return False


def record_audit_tx(cur, action, actor_user_id=None, actor_role=None,
                    target_type=None, target_id=None, slide=None, detail=None,
                    ts=None, slide_id=None):
    """同事务审计写入（cursor 注入变体，PR5 billing 写路径专用）。

    与 record_audit 的关键差异：**不吞错**——任何失败直接抛出，让调用方的
    业务事务（billing caps 更新 / 人工调账入账）整体回滚（方案 §6.5「写入
    ledger 与 audit event 必须同一 PostgreSQL 事务提交」；PR2 ingest_usage_event
    已确立同一原则）。不进 dispatcher 公共名：json 后端没有「同事务」概念，
    billing 系调用方（billing_store）本就 PG-only 直连本模块。
    """
    ev_id = "aud_" + secrets.token_hex(16)
    detail = dict(detail) if isinstance(detail, dict) else {}
    cur.execute(
        "INSERT INTO audit_events "
        "(event_id, ts, actor_user_id, actor_role, action, "
        " target_type, target_id, slide, detail, slide_id) "
        "VALUES (%s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s)",
        (ev_id, ts if ts is not None else time.time(),
         actor_user_id or None, actor_role or "",
         str(action or ""), target_type or None, target_id or None,
         slide or None, psycopg.types.json.Jsonb(detail), slide_id or None),
    )
    return ev_id


def list_audit(limit=50, offset=0, action=None):
    """返回审计事件（最新在前），支持分页与 action 过滤。owner-only 消费（app.py 鉴权）。"""
    limit = max(0, int(limit if limit is not None else 50))
    offset = max(0, int(offset if offset is not None else 0))
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if action:
                    cur.execute(
                        "SELECT event_id, actor_user_id, actor_role, action, "
                        " target_type, target_id, slide, detail, "
                        " extract(epoch from ts)::float8 AS ts "
                        "FROM audit_events WHERE action=%s "
                        "ORDER BY ts DESC, event_id DESC LIMIT %s OFFSET %s",
                        (action, limit, offset))
                else:
                    cur.execute(
                        "SELECT event_id, actor_user_id, actor_role, action, "
                        " target_type, target_id, slide, detail, "
                        " extract(epoch from ts)::float8 AS ts "
                        "FROM audit_events "
                        "ORDER BY ts DESC, event_id DESC LIMIT %s OFFSET %s",
                        (limit, offset))
                rows = cur.fetchall()
        out = []
        for r in rows:
            ev = {
                "id": r["event_id"],
                "ts": r["ts"],
                "actor_user_id": r["actor_user_id"],
                "actor_role": r["actor_role"],
                "action": r["action"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "slide": r["slide"],
                "detail": r["detail"] or {},
            }
            out.append(ev)
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 项目归档只读开关（docs §v1.5）
# --------------------------------------------------------------------------- #
def set_project_archived(pid, archived):
    """设置项目 archived 纯只读开关。返回更新后的项目 dict；不存在返回 None。"""
    archived_b = bool(archived)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("UPDATE projects SET archived=%s WHERE project_id=%s",
                            (archived_b, pid))
                if cur.rowcount == 0:
                    return None
                return _fetch_project(cur, pid)
    finally:
        conn.close()


def archived_slide_names():
    """返回属于任意 archived 项目的切片名集合（归档只读判定用）。

    P0-W2（审计 P0-2）：owner 一致性联接——成员切片 owner 与项目 owner
    双方已知且不一致（毒化成员行：他人把我的切片挂进他的项目再归档）时
    不计入归档集合（我的切片不被他人项目归档锁死）；任一方 NULL / 名行
    不可解析（本地单租户与历史行）维持旧行为照旧锁定。slide_id 为权威
    键，NULL-ID 名行走 legacy_filename 回退解析。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT ps.slide FROM project_slides ps "
                    "JOIN projects p ON p.project_id=ps.project_id "
                    "LEFT JOIN slides s ON s.slide_id=ps.slide_id "
                    "OR (ps.slide_id IS NULL AND s.legacy_filename=ps.slide) "
                    "WHERE p.archived=TRUE "
                    "AND (s.owner_user_id IS NULL OR p.owner_user_id IS NULL "
                    "OR s.owner_user_id=p.owner_user_id)")
                return {r["slide"] for r in cur.fetchall()}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 插件安装凭证（plugin_installations）—— Stage 4-1a（docs §7.6 / §6.2）
#
# 语义与 json 实现完全一致（secret 只存 sha256 hash；_installation_out 剥离
# hash 的导出形状直接复用 json 侧私有助手，避免两份漂移）。表结构见
# migrations/0005_plugin.sql。
# --------------------------------------------------------------------------- #
def _fetch_installation(cur, installation_id):
    cur.execute(
        "SELECT installation_id, plugin_id, version, enabled, secret_hash, "
        " capabilities, approved_scopes, "
        " extract(epoch from created_at)::float8 AS created_at, "
        " extract(epoch from disabled_at)::float8 AS disabled_at "
        "FROM plugin_installations WHERE installation_id=%s",
        (installation_id,))
    return cur.fetchone()


def create_plugin_installation(plugin_id, version="", secret=None,
                               capabilities=None, approved_scopes=None):
    """创建插件安装行，返回 {**installation, "secret": 明文}（仅此一次）。

    capabilities 为可选的能力注册表登记项（docs §4.1；缺省 []）。
    approved_scopes 为可选的已批准扩展权限列表（C5 合同 §2.1；缺省 []）。
    """
    if not isinstance(plugin_id, str) or not plugin_id.strip():
        raise ValueError("plugin_id 不能为空")
    plaintext = secret if isinstance(secret, str) and secret else (
        "pin_" + secrets.token_urlsafe(32))
    installation_id = "pin_" + secrets.token_urlsafe(12)
    now = time.time()
    caps_json = json.dumps(
        [dict(c) for c in capabilities] if isinstance(capabilities, list) else [])
    scopes = [str(s) for s in (approved_scopes or []) if s]
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO plugin_installations "
                    "(installation_id, plugin_id, version, enabled, secret_hash, "
                    " capabilities, approved_scopes, created_at) "
                    "VALUES (%s,%s,%s,TRUE,%s,%s,%s, to_timestamp(%s))",
                    (installation_id, plugin_id.strip(), version or "",
                     _hash_installation_secret(plaintext), caps_json, scopes,
                     now))
                row = _fetch_installation(cur, installation_id)
        out = _installation_out(dict(row))
        out["secret"] = plaintext
        return out
    finally:
        conn.close()


def rotate_installation_secret(installation_id, secret=None):
    """轮换安装凭证：旧 secret 立即失效，返回带新明文的一次性 dict；无则 None。"""
    plaintext = secret if isinstance(secret, str) and secret else (
        "pin_" + secrets.token_urlsafe(32))
    new_hash = _hash_installation_secret(plaintext)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE plugin_installations SET secret_hash=%s "
                    "WHERE installation_id=%s",
                    (new_hash, installation_id))
                if cur.rowcount == 0:
                    return None
                row = _fetch_installation(cur, installation_id)
        out = _installation_out(dict(row))
        out["secret"] = plaintext
        return out
    finally:
        conn.close()


def get_plugin_installation(installation_id):
    """按 installation_id 取安装行（不含 secret_hash）；无则 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                row = _fetch_installation(cur, installation_id)
        return _installation_out(dict(row)) if row else None
    finally:
        conn.close()


def verify_installation_secret(installation_id, secret):
    """校验安装凭证（常数时间比较）；行不存在或 hash 不一致返回 False。"""
    if not isinstance(secret, str) or not secret:
        return False
    candidate = _hash_installation_secret(secret)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT secret_hash FROM plugin_installations "
                    "WHERE installation_id=%s", (installation_id,))
                row = cur.fetchone()
        if row is None:
            return False
        return hmac.compare_digest(str(row["secret_hash"] or ""), candidate)
    finally:
        conn.close()


def set_installation_enabled(installation_id, enabled):
    """启/禁安装（禁用即撤销该安装全部在途 JWT）；不存在返回 None。"""
    enabled_b = bool(enabled)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if enabled_b:
                    cur.execute(
                        "UPDATE plugin_installations SET enabled=TRUE, "
                        "disabled_at=NULL WHERE installation_id=%s",
                        (installation_id,))
                else:
                    cur.execute(
                        "UPDATE plugin_installations SET enabled=FALSE, "
                        "disabled_at=now() WHERE installation_id=%s",
                        (installation_id,))
                if cur.rowcount == 0:
                    return None
                row = _fetch_installation(cur, installation_id)
        return _installation_out(dict(row))
    finally:
        conn.close()


def list_plugin_installations():
    """列出全部安装行（不含 hash），按创建时间升序。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT installation_id, plugin_id, version, enabled, "
                    " capabilities, approved_scopes, "
                    " extract(epoch from created_at)::float8 AS created_at, "
                    " extract(epoch from disabled_at)::float8 AS disabled_at "
                    "FROM plugin_installations ORDER BY created_at ASC")
                rows = cur.fetchall()
        return [_installation_out(dict(r)) for r in rows]
    finally:
        conn.close()


def set_installation_capabilities(installation_id, capabilities):
    """整体替换安装行的能力注册表（docs §4.1；语义同 json 实现）。

    返回更新后的安装行（不含 hash）；不存在返回 None。
    """
    caps_json = json.dumps(
        [dict(c) for c in capabilities] if isinstance(capabilities, list) else [])
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE plugin_installations SET capabilities=%s "
                    "WHERE installation_id=%s",
                    (caps_json, installation_id))
                if cur.rowcount == 0:
                    return None
                row = _fetch_installation(cur, installation_id)
        return _installation_out(dict(row))
    finally:
        conn.close()


def set_installation_approved_scopes(installation_id, approved_scopes):
    """整体替换安装行的批准权限面（C5 合同 §2.1；0077 列 approved_scopes）。

    值 = 字符串列表（经 MANIFEST_APPROVAL_REQUIRED_PERMISSIONS 枚举校验的
    调用方传入）；空列表 = 未批准任何扩展权限（存量行缺省语义）。返回更新
    后的安装行（不含 hash）；不存在返回 None。"""
    scopes = [str(s) for s in (approved_scopes or []) if s]
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE plugin_installations SET approved_scopes=%s "
                    "WHERE installation_id=%s",
                    (scopes, installation_id))
                if cur.rowcount == 0:
                    return None
                row = _fetch_installation(cur, installation_id)
        return _installation_out(dict(row))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# run grant（run_grants）—— Stage 4-1a（docs §7.6）
# --------------------------------------------------------------------------- #
_GRANT_SEL = (
    "SELECT grant_id, installation_id, slide, session_id, created_by_user_id, "
    " slide_id, "
    " allow_ai_drawing, "
    " extract(epoch from drawing_bound_at)::float8 AS drawing_bound_at, "
    " drawing_intent, "
    " extract(epoch from created_at)::float8 AS created_at, "
    " extract(epoch from expires_at)::float8 AS expires_at, revoked, "
    " extract(epoch from revoked_at)::float8 AS revoked_at "
)


def _fetch_grant(cur, grant_id):
    cur.execute(_GRANT_SEL + "FROM run_grants WHERE grant_id=%s", (grant_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def create_run_grant(installation_id, slide, session_id="",
                     created_by_user_id=None, ttl_seconds=None, slide_id=None):
    """发放一条 run grant（默认 2h），返回 grant dict。

    P2（合同 §3.3/R-09）：slide_id（权威）+ slide（名称快照）双写；slide_id
    未显式给出时按 legacy 名解析（解析不到保持 NULL——历史无行形态，授权
    校验侧对 NULL 行回退名比对，随 TTL 自然退役）。
    """
    if not isinstance(installation_id, str) or not installation_id:
        raise ValueError("installation_id 不能为空")
    if not isinstance(slide, str) or not slide:
        raise ValueError("slide 不能为空")
    try:
        ttl = float(ttl_seconds) if ttl_seconds is not None else 7200.0
    except (TypeError, ValueError):
        ttl = 7200.0
    if ttl <= 0:
        ttl = 7200.0
    now = time.time()
    grant_id = "rgr_" + secrets.token_urlsafe(12)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if slide_id is None:
                    slide_id = _slide_id_of_name(cur, slide)
                # 描绘默认随 grant 发放（是否画由模型按本轮消息决定）；写权限
                # 仍由 annotate 路径按 grant 与创建者当前权限检查。
                cur.execute(
                    "INSERT INTO run_grants "
                    "(grant_id, installation_id, slide, session_id, "
                    " created_by_user_id, slide_id, created_at, expires_at, "
                    " allow_ai_drawing, drawing_bound_at, drawing_intent) "
                    "VALUES (%s,%s,%s,%s,%s,%s, to_timestamp(%s), to_timestamp(%s), "
                    " TRUE, to_timestamp(%s), 'default')",
                    (grant_id, installation_id, slide, session_id or "",
                     created_by_user_id or None, slide_id or None,
                     now, now + ttl, now))
                return _fetch_grant(cur, grant_id)
    finally:
        conn.close()


def get_run_grant(grant_id):
    """按 grant_id 取 grant dict；无则 None。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _fetch_grant(cur, grant_id)
    finally:
        conn.close()


def revoke_run_grant(grant_id):
    """撤销 run grant（幂等）。返回是否找到（已撤销也算 True）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE run_grants SET revoked=TRUE, revoked_at=now() "
                    "WHERE grant_id=%s", (grant_id,))
                return cur.rowcount > 0
    finally:
        conn.close()


def list_run_grants_for_session(session_id):
    """列出某 session_id 的全部 grant（按创建时间升序）；空 session_id 返回空。"""
    if not session_id:
        return []
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    _GRANT_SEL + "FROM run_grants WHERE session_id=%s "
                    "ORDER BY created_at ASC", (session_id,))
                return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def bind_run_grant_session(grant_id, session_id):
    """§3.10 P0-C：把 grant 原子绑定到 session_id（CAS，同 json 语义）。

    UPDATE ... WHERE grant_id=%s AND (session_id='' OR session_id IS NULL) 的
   原子 CAS；返回绑定后的 grant dict；不存在返回 None；已绑定到其它 session
   → ValueError("session_mismatch")；同一 session 重复绑定幂等。
    """
    if not isinstance(grant_id, str) or not grant_id:
        raise ValueError("grant_id 不能为空")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id 不能为空")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE run_grants SET session_id=%s "
                    "WHERE grant_id=%s AND (session_id='' OR session_id IS NULL)",
                    (session_id, grant_id))
                if cur.rowcount == 0:
                    row = _fetch_grant(cur, grant_id)
                    if row is None:
                        return None
                    bound = row.get("session_id") or ""
                    if bound and bound != session_id:
                        raise ValueError("session_mismatch")
                    return row  # 幂等：已绑定到同一 session
                return _fetch_grant(cur, grant_id)
    finally:
        conn.close()


_DRAWING_INTENTS = ("explicit", "model", "natural", "default")


def set_run_grant_skill_delineate(grant_id, session_id, intent,
                                  installation_id=None, creator_allowed=None):
    """描绘技能置位（0082 / 契约 §3.2，裁决 5）：run grant 的
    allow_ai_drawing 仅 false→true 的 CAS，幂等；收回只靠 revoke/expiry。

    同一事务内（SELECT ... FOR UPDATE 行锁）依次复查：
      1. grant 存在（grant_not_found）；
      2. installation 匹配（installation_id 给出且不符 → installation_mismatch）；
      3. 未撤销（grant_revoked）、未过期（grant_expired）；
      4. 已绑定到 session_id 同一 session（grant_unbound / session_mismatch——
         bind 端点先于本置位，与 runMain preamble 顺序一致）；
      5. creator_allowed(grant) 创建者写权限复查（app 层注入
         _run_grant_creator_allowed 同口径；回调抛异常按不允许 fail-closed →
         creator_not_allowed）。
    全部通过且当前 false → UPDATE true + drawing_bound_at + drawing_intent；
    已 true → no-op（幂等返回当前行）。任何失败路径零副作用（事务内无写）。

    返回 (ok, reason)：ok=True 表示已置位/已为 true；reason 为稳定码
    （grant_not_found/installation_mismatch/grant_revoked/grant_expired/
    grant_unbound/session_mismatch/creator_not_allowed），端点据此映射 HTTP。
    intent 形态由调用方（端点）校验；这里再守一层白名单。
    """
    if not isinstance(grant_id, str) or not grant_id:
        raise ValueError("grant_id 不能为空")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id 不能为空")
    if intent not in _DRAWING_INTENTS:
        raise ValueError("intent 需为 explicit|model|natural")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(_GRANT_SEL + "FROM run_grants "
                            "WHERE grant_id=%s FOR UPDATE", (grant_id,))
                row = cur.fetchone()
                if row is None:
                    return False, "grant_not_found"
                grant = dict(row)
                if installation_id and grant.get("installation_id") \
                        != installation_id:
                    return False, "installation_mismatch"
                if grant.get("revoked"):
                    return False, "grant_revoked"
                try:
                    expired = float(grant.get("expires_at") or 0) <= time.time()
                except (TypeError, ValueError):
                    expired = True
                if expired:
                    return False, "grant_expired"
                bound = grant.get("session_id") or ""
                if not bound:
                    return False, "grant_unbound"
                if bound != session_id:
                    return False, "session_mismatch"
                if creator_allowed is not None:
                    try:
                        allowed = bool(creator_allowed(grant))
                    except Exception:
                        _LOG.warning("run grant 技能置位的创建者复查失败"
                                     "（按不允许处理）", exc_info=True)
                        allowed = False
                    if not allowed:
                        return False, "creator_not_allowed"
                if grant.get("allow_ai_drawing") is True:
                    return True, ""  # 幂等：已置位
                cur.execute(
                    "UPDATE run_grants SET allow_ai_drawing=TRUE, "
                    "drawing_bound_at=now(), drawing_intent=%s "
                    "WHERE grant_id=%s", (intent, grant_id))
                return True, ""
    finally:
        conn.close()


def list_run_grants(slide=None, include_revoked=False, slide_id=None):
    """列出 run grant（§3.10 P0-C 主动撤销钩子用；按创建时间升序）。

    P2：slide_id 给出时按 ID 过滤（活动查询口径）；仅名时解析到 ID 后取
    「该 ID 的行 ∪ 历史 NULL-ID 同名行」——撤销钩子必须覆盖全部行，不因
    双写口径漏撤。
    """
    sql = _GRANT_SEL + "FROM run_grants"
    conds, params = [], []
    if slide_id is not None:
        conds.append("slide_id=%s")
        params.append(slide_id)
    elif slide is not None:
        sid = None
        conn0 = _connect()
        try:
            with pg_store.transaction(conn0) as c0:
                with c0.cursor() as cur0:
                    sid = _slide_id_of_name(cur0, slide)
        finally:
            conn0.close()
        if sid is not None:
            conds.append("(slide_id=%s OR (slide_id IS NULL AND slide=%s))")
            params.extend((sid, slide))
        else:
            conds.append("slide=%s")
            params.append(slide)
    if not include_revoked:
        conds.append("revoked=FALSE")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY created_at ASC"
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(sql, tuple(params))
                return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 会话级「允许 AI 描绘」开关——PT 本地镜像（0039 / P1-4）
#   权威在 HP 侧 session 文件；PT 只存代理路由（/api/ai/session/<sid>/drawing）
#   回传的权威布尔值。polygon/freehand 写入口查本表：无行或 false 一律拒绝
#   （fail closed——镜像没见过该 session 就是不允许）。
# --------------------------------------------------------------------------- #
def get_ai_session_drawing_flag(session_id):
    """读 session 的镜像开关。

    返回 True/False（行存在时）；**行不存在返回 None**（与 False 区分，调用方
    必须对 None 一并 fail closed，不得把"没见过"当成"允许"）。
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT allow_ai_drawing FROM ai_session_drawing_flags "
                    "WHERE session_id=%s", (session_id,))
                row = cur.fetchone()
                return bool(row["allow_ai_drawing"]) if row is not None else None
    finally:
        conn.close()


def upsert_ai_session_drawing_flag(session_id, allow_ai_drawing):
    """upsert session 的镜像开关（无条件写，generation 自增）。幂等值、不幂等代。

    allow_ai_drawing 必须是布尔（代理侧已校验；这里再守一层）。
    返回写入后的 generation（int，从 1 起单调自增）——三轮 review P1：
    关闭预写调用方以此作为响应回程 CAS 的基线；任何无条件写都会作废所有
    携带更早基线的在途响应。
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id 不能为空")
    if not isinstance(allow_ai_drawing, bool):
        raise ValueError("allow_ai_drawing 需为布尔")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO ai_session_drawing_flags "
                    "(session_id, allow_ai_drawing, updated_at, generation) "
                    "VALUES (%s, %s, now(), 1) "
                    "ON CONFLICT (session_id) DO UPDATE SET "
                    "allow_ai_drawing=EXCLUDED.allow_ai_drawing, "
                    "updated_at=now(), "
                    "generation=ai_session_drawing_flags.generation+1 "
                    "RETURNING generation",
                    (session_id, allow_ai_drawing))
                return int(cur.fetchone()["generation"])
    finally:
        conn.close()


def init_ai_session_drawing_flag(session_id, allow_ai_drawing):
    """仅当行不存在时插入镜像开关。已有行（含用户关闭后的 false）一律不覆盖。

    返回 True=本次插入；False=已有行，未改动。创建参数路径必须走这里，
    不能 reserve+CAS：后者会抬 generation，并在未超越时把已关闭开关写回
    true（同 request_id 去重重放 / 迟到 on_accepted 会重新打开闸门）。
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id 不能为空")
    if not isinstance(allow_ai_drawing, bool):
        raise ValueError("allow_ai_drawing 需为布尔")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO ai_session_drawing_flags "
                    "(session_id, allow_ai_drawing, updated_at, generation) "
                    "VALUES (%s, %s, now(), 1) "
                    "ON CONFLICT (session_id) DO NOTHING "
                    "RETURNING generation",
                    (session_id, allow_ai_drawing))
                return cur.fetchone() is not None
    finally:
        conn.close()


def get_ai_session_drawing_generation(session_id):
    """读镜像行 generation（三轮 review P1）。无行返回 0（第一代之前）。"""
    if not isinstance(session_id, str) or not session_id:
        return 0
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT generation FROM ai_session_drawing_flags "
                    "WHERE session_id=%s", (session_id,))
                row = cur.fetchone()
                return int(row["generation"]) if row is not None else 0
    finally:
        conn.close()


def reserve_ai_session_drawing_generation(session_id):
    """原子占用一个新的 generation（不改 allow 值）。返回占用到的代数（int）。

    五轮 review P1：开启路径此前用 get_generation()+1 推算代数，与紧随的
    关闭预写（无条件自增）可能拿到**同一代**——HP 会把后到的关闭当同代旧
    请求丢弃，造成 PT=false / HP=true 分叉。开启与关闭现在都经原子分配
    （本函数 / 预写 upsert）取得唯一递增代数；占代顺序即请求到达 PT 的顺序。
    无行 session 首次占代插入 allow=false 行（fail-closed：正在开启中的会话
    在写闸看来仍是关）。
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id 不能为空")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO ai_session_drawing_flags "
                    "(session_id, allow_ai_drawing, updated_at, generation) "
                    "VALUES (%s, FALSE, now(), 1) "
                    "ON CONFLICT (session_id) DO UPDATE SET "
                    "updated_at=now(), "
                    "generation=ai_session_drawing_flags.generation+1 "
                    "RETURNING generation",
                    (session_id,))
                return int(cur.fetchone()["generation"])
    finally:
        conn.close()


def cas_ai_session_drawing_flag(session_id, allow_ai_drawing, max_generation):
    """CAS 写镜像：仅当当前 generation <= max_generation 才写入（gen+1）。

    三轮 review P1：代理响应回程的唯一写入口——携带「请求发起时」的基线，
    行已被更晚的请求（更高 generation）写入时 no-op。返回是否生效（bool）。
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id 不能为空")
    if not isinstance(allow_ai_drawing, bool):
        raise ValueError("allow_ai_drawing 需为布尔")
    if isinstance(max_generation, bool) or not isinstance(max_generation, int) \
            or max_generation < 0:
        raise ValueError("max_generation 需为非负整数")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO ai_session_drawing_flags "
                    "(session_id, allow_ai_drawing, updated_at, generation) "
                    "VALUES (%s, %s, now(), 1) "
                    "ON CONFLICT (session_id) DO UPDATE SET "
                    "allow_ai_drawing=EXCLUDED.allow_ai_drawing, "
                    "updated_at=now(), "
                    "generation=ai_session_drawing_flags.generation+1 "
                    "WHERE ai_session_drawing_flags.generation <= %s "
                    "RETURNING generation",
                    (session_id, allow_ai_drawing, max_generation))
                row = cur.fetchone()
                return row is not None
    finally:
        conn.close()
