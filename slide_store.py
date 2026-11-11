# -*- coding: utf-8 -*-
"""slide_store —— 切片资产的 PG 身份/状态权威（slide ID 化重构 P1-A）。

本模块是「代码旁合同」：以下状态机、锁顺序、接口语义镜像自
docs/slide-id-refactor-p1-contract-20260925.md（§3.1/§4/§5）与
docs/slide-id-storage-refactor-agent-plan-20260925.md §2.3；偏离需在 review
中显式裁决。schema 底座是 migrations/0067_slide_asset_identity.sql。
worker 可 import 本模块（不得 import app；本模块只依赖 pg_store/slide_storage）。

身份与 ID（合同 §1）
  - slide_id 沿用现行生成器 ``"sld_" + secrets.token_urlsafe(9)``（12 位
    urlsafe），服务端随机、DB 主键兜底唯一；不从原名/owner/内容哈希推导；
    不重置旧 ID。
  - 一次新的逻辑切片上传 = 一个新 slide_id；幂等重试复用原任务及其 ID
    （任务表 slide_id 列/upload_task_items 是持久绑定，P3 接线）；删除后
    重传用新 ID。
  - original_filename/display_name 只是展示；``legacy_filename`` 是冻结别名：
    新资产恒 NULL；已删除资产的 tombstone 行保留 legacy_filename
    （UNIQUE 约束天然阻止旧别名重绑新 ID）。

状态机与可见性（合同 §4）
::

    allocate_slide
        │
        ▼
     staging ──publish 成功──▶ ready ──request_delete──▶ deleting ──清理+结算完成──▶ deleted
        │                      │                            │
        ├─任务失败/取消─▶ failed│                            └─清理失败：停留 deleting 重试（slide_delete_jobs）
        │   （staging 清理后行可删或留 failed 证据） │
    legacy（0067 默认）──盘点/验证通过──▶ ready（layout 仍 'legacy'，P6 再迁 id_bundle）
    legacy ──验证失败（缺文件/归属歧义）──▶ failed（保留证据，不可读）

  - **DB ``asset_state='ready'`` 是唯一可见性开关**：所有读取入口（含旧端点经
    legacy alias 解析后）同受 authorize_read 门禁。
  - ``deleting`` 立即拒绝新读取授权；已取得的受控文件句柄可完成当次读取。
  - 不允许同一 slide_id 覆盖内容；换内容 = 新 slide_id。

锁顺序（合同 §5，全仓统一；任何新代码取多把锁必须按此顺序）::
::

    pg_advisory_xact_lock(hashtext('slide:' || slide_id))   ← 所有 publish/delete/recovery 的第一把锁（跨进程仲裁点；
                                                               'slide:' 前缀与既有 hashtext(job_id) 键空间隔离）
      → 任务行锁（upload_tasks / ingestion_jobs / conversion_jobs 行，SELECT ... FOR UPDATE）
      → slides 行锁
      → upload_reservations 行
      → upload_user_quotas 行
      → cos_pool_state 行

  已审计无环（合同 §5）：slides 行锁插在任务行之后不引入反向边；delete 路径
  只持 advisory+slides 行，不回头取任务锁。禁止「stat 相同就 unlink/转 owner」。

接口语义要点
  - ``allocate_slide``：创建 staging/id_bundle 资产行（storage_relpath 由
    slide_storage.entry_relpath 服务端派生）；**不查原名是否已存在**；owner
    必须显式（无 UID 的本地模式由调用方先解析配置 owner，不允许空 owner
    自动认领）。
  - ``resolve_slide_id``：校验存在性；缺失不凭 ``sld_`` 前缀当成功。
  - ``resolve_legacy_alias``：仅查 legacy_filename 冻结映射，再进入同一
    resolver 语义；无文件系统猜测。
  - ``authorize_read``：统一门禁（见函数 docstring 的权限序）；DB 异常按
    拒绝处理，不回退目录扫描。
  - 状态迁移原语（mark_ready/request_delete/mark_deleted/mark_failed）全部
    是带 ``expected_state`` 谓词的真实 SQL CAS，返回是否迁移成功。
    ``request_delete``（P5 合同 §1.1）与 ``slide_delete_jobs`` 落库同一事务；
    ``mark_deleted`` 的结算幂等键 = deleting→deleted CAS 本身（同事务减账）。
    删除任务原语（enqueue/claim/finish，lease+退避）见「删除任务持久化」节。
  - 名称/元数据编辑（update_display_name/update_note/set_public）只动
    元数据：不动文件、不动 legacy_filename、不动授权。
"""

import contextlib
import logging
import re
import secrets
import time
from dataclasses import dataclass

import psycopg

import pg_store
import slide_storage

_LOG = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
class SlideState:
    """slides.asset_state 的合法值（合同 §2.1/§4；与 0067 CHECK 一致）。"""

    STAGING = "staging"      # 已分配 ID、尚未发布（不可读）
    LEGACY = "legacy"        # 0067 默认：待验证历史资产（不可读，回填脚本迁移）
    READY = "ready"          # 已发布：唯一可读状态
    DELETING = "deleting"    # 已请求删除：立即拒绝新读取授权
    DELETED = "deleted"      # 物理清理+结算完成（tombstone；保留 legacy_filename）
    FAILED = "failed"        # 任务失败/验证失败（保留证据，不可读）

    ALL = frozenset({
        STAGING, LEGACY, READY, DELETING, DELETED, FAILED,
    })


class StorageLayout:
    """slides.storage_layout 的合法值（合同 §2.1）。"""

    LEGACY = "legacy"        # UPLOAD_DIR 根下历史平铺布局（过渡，R-16）
    ID_BUNDLE = "id_bundle"  # objects/<slide_id>/ 独占包布局


#: 平台管理角色（admin 语义 = user_store.ROLE_OWNER；本模块不 import
#: user_store，保持 worker 依赖最小，值由 0001 的角色词表固定为 'owner'）。
ROLE_ADMIN = "owner"

#: format_ext 白名单归一：小写字母数字、1..16 位（真实格式白名单校验在
#: 上传通道的格式注册器完成；此处是 DB 侧兜底归一）。
_FORMAT_EXT_RE = re.compile(r"^[a-z0-9]{1,16}$")

#: original_filename / display_name 的服务端边界（R-19：不接受目录语义、
#: 限制长度/控制字符；输出转义由 HTTP 层负责，防 CRLF 注入）。
_MAX_FILENAME_BYTES = 255
_MAX_DISPLAY_NAME_LEN = 200
_MAX_NOTE_LEN = 500


# --------------------------------------------------------------------------- #
# 连接（share_store_pg 同款：pg_store.connect + dict_row）
# --------------------------------------------------------------------------- #
def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


@contextlib.contextmanager
def _session(conn=None):
    """调用方事务复用：传入 conn 则原样 yield（不 commit/不关）；否则自建
    连接 + pg_store.transaction（commit on success / rollback on error）。"""
    if conn is not None:
        yield conn
        return
    c = _connect()
    try:
        with pg_store.transaction(c):
            yield c
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# descriptor
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SlideDescriptor:
    """切片资产的受控视图（合同 §3.1）。

    路径字段（storage_relpath）仅内部使用，**绝不序列化进 HTTP 响应**
    （R-20）；legacy_filename 是冻结别名，仅兼容解析用。
    """

    slide_id: str
    owner_user_id: str | None
    original_filename: str | None
    display_name: str
    legacy_filename: str | None
    format_ext: str | None
    asset_state: str
    storage_layout: str
    storage_relpath: str | None
    accounted_bytes: int | None
    public: bool
    note: str
    published_at: float | None
    deleted_at: float | None
    revision: str | None


_DESCRIPTOR_SQL = """
SELECT s.slide_id, s.owner_user_id, s.original_filename, s.display_name,
       s.legacy_filename, s.format_ext, s.asset_state, s.storage_layout,
       s.storage_relpath, s.accounted_bytes, s.public, s.note,
       extract(epoch from s.published_at)::float8 AS published_at,
       extract(epoch from s.deleted_at)::float8 AS deleted_at,
       (SELECT a.legacy_revision FROM slide_assets a
         WHERE a.slide_id = s.slide_id
         ORDER BY a.created_at DESC, a.asset_id DESC LIMIT 1) AS revision
  FROM slides s
"""


def _row_to_descriptor(row):
    if row is None:
        return None
    return SlideDescriptor(
        slide_id=row["slide_id"],
        owner_user_id=row["owner_user_id"],
        original_filename=row["original_filename"],
        display_name=row["display_name"] or "",
        legacy_filename=row["legacy_filename"],
        format_ext=row["format_ext"],
        asset_state=row["asset_state"],
        storage_layout=row["storage_layout"],
        storage_relpath=row["storage_relpath"],
        accounted_bytes=(int(row["accounted_bytes"])
                         if row["accounted_bytes"] is not None else None),
        public=bool(row["public"]),
        note=row["note"] or "",
        published_at=row["published_at"],
        deleted_at=row["deleted_at"],
        revision=row["revision"],
    )


# --------------------------------------------------------------------------- #
# 输入归一（R-19 / R-02）
# --------------------------------------------------------------------------- #
def _new_slide_id() -> str:
    """沿用现行生成器（合同 §1；share_store_pg._new_slide_id 同款）。"""
    return "sld_" + secrets.token_urlsafe(9)  # 12 位 urlsafe


def sanitize_original_filename(name: str) -> str:
    """original_filename 展示快照的服务端边界（R-19）。

    不接受目录语义（含 ``/``、``\\``）、拒绝控制字符/空串/超长（>255 字节
    UTF-8）。违规抛 ValueError——上传通道应先做白名单校验，这里是资产层的
    最后闸门，绝不静默截取 basename。
    """
    if not isinstance(name, str):
        raise ValueError("original_filename 必须是字符串")
    s = name.strip()
    if not s:
        raise ValueError("original_filename 不能为空")
    if len(s.encode("utf-8")) > _MAX_FILENAME_BYTES:
        raise ValueError("original_filename 超过 %d 字节" % _MAX_FILENAME_BYTES)
    if "/" in s or "\\" in s:
        raise ValueError("original_filename 不接受目录语义")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in s):
        raise ValueError("original_filename 含控制字符")
    return s


def normalize_format_ext(ext) -> str:
    """format_ext 归一：去点、小写、白名单 ``^[a-z0-9]{1,16}$``（DB 兜底）。"""
    if not isinstance(ext, str):
        raise ValueError("format_ext 必须是字符串")
    e = ext.strip().lower().lstrip(".")
    if not _FORMAT_EXT_RE.match(e):
        raise ValueError("format_ext 非白名单小写扩展名：%r" % (ext,))
    return e


def _clean_display_name(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("display_name 必须是字符串")
    s = value.strip()
    if len(s) > _MAX_DISPLAY_NAME_LEN:
        raise ValueError("display_name 超过 %d 字符" % _MAX_DISPLAY_NAME_LEN)
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in s):
        raise ValueError("display_name 含控制字符")
    return s


def _clean_note(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("note 必须是字符串")
    s = value.strip()
    if len(s) > _MAX_NOTE_LEN:
        raise ValueError("note 超过 %d 字符" % _MAX_NOTE_LEN)
    return s


# --------------------------------------------------------------------------- #
# 资产分配与解析（合同 §3.1）
# --------------------------------------------------------------------------- #
def allocate_slide(owner_user_id, original_filename, format_ext, *,
                   display_name=None, note=None, conn=None) -> SlideDescriptor:
    """分配一个新切片资产行（asset_state='staging'、storage_layout='id_bundle'）。

    合同要点（§1/§3.1）：
      - **一次新的逻辑切片上传 = 一个新 slide_id**：本函数不查原名是否已
        存在——同账号/不同账号上传同名、同内容文件都是独立资产；幂等重试
        复用原任务及其 ID 的职责在任务表绑定（upload_tasks.slide_id /
        upload_task_items，P3 接线），不在本函数。
      - owner 必须显式非空：无 UID 的本地模式由调用方先解析配置 owner，
        不允许空 owner 自动认领（违规抛 ValueError）。
      - storage_relpath 由 slide_storage.entry_relpath 服务端派生
        （``objects/<slide_id>/data.<format_ext>``），唯一且不可变（R-20），
        客户端不可设置；legacy_filename 恒 NULL（新资产不写冻结别名）。
      - display_name 初始 = original_filename（R-02）。
    """
    if not isinstance(owner_user_id, str) or not owner_user_id.strip():
        raise ValueError("allocate_slide 需要显式 owner_user_id（不允许空 owner）")
    owner = owner_user_id.strip()
    orig = sanitize_original_filename(original_filename)
    ext = normalize_format_ext(format_ext)
    disp = _clean_display_name(display_name) or orig
    note_clean = _clean_note(note)

    with _session(conn) as c:
        slide_id = _new_slide_id()
        relpath = slide_storage.entry_relpath(slide_id, ext)
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO slides (slide_id, original_filename, display_name, "
                "note, owner_user_id, format_ext, asset_state, storage_layout, "
                "storage_relpath) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (slide_id, orig, disp, note_clean, owner, ext,
                 SlideState.STAGING, StorageLayout.ID_BUNDLE, relpath),
            )
        return _fetch_descriptor(c, slide_id)


def _fetch_descriptor(conn, slide_id):
    """按主键取 descriptor（conn 是 _session yield 出的连接/事务）。"""
    with conn.cursor() as cur:
        cur.execute(_DESCRIPTOR_SQL + " WHERE s.slide_id=%s", (slide_id,))
        return _row_to_descriptor(cur.fetchone())


def resolve_slide_id(slide_id, *, conn=None):
    """slide_id → SlideDescriptor | None。

    校验存在性：缺失/未知 ID 返回 None，**不凭 ``sld_`` 前缀当成功**
    （合同 §3.1；与 share_store_pg.resolve_slide_ref 的前缀直返语义不同，
    那是 P1 起仅限迁移兼容层使用的旧口径）。本函数只解析、不鉴权——
    可见性门禁统一在 authorize_read。
    """
    if not isinstance(slide_id, str) or not slide_id.strip():
        return None
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(_DESCRIPTOR_SQL + " WHERE s.slide_id=%s",
                        (slide_id.strip(),))
            return _row_to_descriptor(cur.fetchone())


def resolve_legacy_alias(alias, *, conn=None):
    """冻结别名 → SlideDescriptor | None（合同 §3.1 / R-01）。

    仅查 ``slides.legacy_filename`` 冻结映射（新资产恒 NULL），无文件系统
    猜测、不做原名目录扫描；解析结果进入与 resolve_slide_id 完全相同的
    descriptor 语义（可见性仍由 authorize_read 门禁裁决）。旧值永不重绑
    新 ID——tombstone 行保留 legacy_filename，UNIQUE 约束兜底。
    """
    if not isinstance(alias, str) or not alias.strip():
        return None
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(_DESCRIPTOR_SQL + " WHERE s.legacy_filename=%s",
                        (alias.strip(),))
            return _row_to_descriptor(cur.fetchone())


def list_ready_descriptors(*, conn=None):
    """asset_state='ready' 的全部 descriptor（P1-B2 列表源；按 legacy_filename 排序）。

    可见性开关的列表面：调用方（app 列表/管理清单）仍需对每行跑
    authorize_read——本函数只保证状态过滤；文件存在性检查由调用方按
    storage_layout stat（legacy 布局入口文件消失即不列出，旧行为）。
    """
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(_DESCRIPTOR_SQL
                        + " WHERE s.asset_state=%s"
                        " ORDER BY s.legacy_filename NULLS LAST",
                        (SlideState.READY,))
            rows = cur.fetchall()
    return [d for d in (_row_to_descriptor(r) for r in rows) if d is not None]


def list_all_descriptors(*, conn=None):
    """全部 slides 行（含非 ready 状态与无 legacy_filename 的行）的 descriptor。

    管理面（admin inventory）的 DB 清单源（P1-B2）：**不做可见性过滤**——
    管理台是唯一「看全部」出口；无 legacy_filename 的行排最后（管理清单的
    name 键是 legacy_filename，id_bundle-only 行由 slide_id 单独呈现）。
    """
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(_DESCRIPTOR_SQL
                        + " ORDER BY s.legacy_filename NULLS LAST")
            rows = cur.fetchall()
    return [d for d in (_row_to_descriptor(r) for r in rows) if d is not None]


# --------------------------------------------------------------------------- #
# 统一读门禁（合同 §3.1/§4）
# --------------------------------------------------------------------------- #
def authorize_read(desc, *, actor_user_id=None, actor_role=None,
                   demo_capability=False, allow_public=True, allow_share=True,
                   conn=None) -> bool:
    """统一读取门禁：所有读取入口（新旧端点、分享进程、插件、AI）共用。

    判定序（合同 §3.1，顺序即裁决优先级）：
      0. ``asset_state == 'ready'`` 且 ``storage_layout == 'id_bundle'`` 是
         唯一可见性开关——staging/legacy 未回填/deleting/deleted/failed 一律
         拒；P6 运行时退役起 legacy 布局行同样拒（待迁移/隔离，读路径只认
         id_bundle；迁移工具经 slide_storage.resolve_legacy_path_for_migration
         专用入口，不经本门禁）。
         **门禁以 DB 当前行值为准**（重读 asset_state/owner/public/layout——
         descriptor 只是解析快照，防快照过期/并发置 deleting 后仍放行）。
      1. admin 角色（平台 owner，管理面读）；
      2. owner（actor_user_id == 当前行 owner_user_id）；
      3. public（当前行值；``allow_public=False`` 时跳过）；
      4. slide_id 级 slide_view_grants（**只认 slide_id 列**，不认
         slide_name——R-06：旧名授权不再匹配新内容）；
      5. share_slides ⋈ grants：已领取（grants.active）未撤销
         （shares.revoked=false）未过期（expires_at NULL 或 > now）且 grant
         含 view 权限；shares.slides JSONB 快照**不参与**判定（R-04）；
         ``allow_share=False`` 时跳过；
      6. demo_catalog capability（调用方显式声明 demo 通道；allowlist
         命中才放行——R-10：删除后 capability 不能读）。

    fail-closed：任何 DB 异常按拒绝处理，不回退目录扫描/名称猜测。
    desc 可传 SlideDescriptor 或 slide_id 字符串（内部走 resolve_slide_id，
    缺失即拒）。

    P1-B2 平台兼容参数（app.py 读隔离接线，P2 权限关系统一后退役）：
      - ``allow_public=False``：认证 owner 的可见集不含他人 public 切片
        （升级 B R5）；Demo 通道亦用（allowlist 唯一入口，public ≠ 匿名可见）。
      - ``allow_share=False``：认证 owner 的可见集不含 claimed 协作切片
        （升级 B R5：owner = 本人 ∪ 显式添加）。缺省两参均为 True（本模块
        既有调用方/测试语义不变）。
    """
    try:
        if desc is None:
            return False
        if isinstance(desc, str):
            desc = resolve_slide_id(desc, conn=conn)
            if desc is None:
                return False
        # 0) 唯一可见性开关——以 DB 当前行值为准（重读防快照过期）
        with _session(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT asset_state, owner_user_id, public, storage_layout "
                    "FROM slides WHERE slide_id=%s", (desc.slide_id,))
                row = cur.fetchone()
        if row is None or row["asset_state"] != SlideState.READY:
            return False
        if row["storage_layout"] != StorageLayout.ID_BUNDLE:
            # P6 运行时退役：legacy 布局行 = 待迁移/隔离——统一门禁层不可见
            # 不可读（读路径只认 id_bundle；迁移工具经专用入口，不经本门禁）。
            return False
        # 1) admin 角色
        if actor_role == ROLE_ADMIN:
            return True
        uid = (actor_user_id or "").strip() or None
        # 2) owner
        if uid and row["owner_user_id"] and uid == row["owner_user_id"]:
            return True
        # 3) public（P1-B2 兼容开关：认证 owner / Demo 通道跳过）
        if allow_public and row["public"]:
            return True
        # 4~5) 显式授权/share 成员：需要具体主体
        if uid:
            if _has_slide_view_grant(uid, desc.slide_id, conn=conn):
                return True
            if allow_share and _has_active_share_membership(
                    uid, desc.slide_id, conn=conn):
                return True
        # 6) demo capability（独立于主体——匿名 demo 通道也走 allowlist，
        #    命中才放行）
        if demo_capability and _in_demo_catalog(desc.slide_id, conn=conn):
            return True
        return False
    except Exception:  # noqa: BLE001 - DB 异常按拒处理（fail-closed）
        _LOG.warning("authorize_read fail-closed（slide_id=%s）",
                     getattr(desc, "slide_id", desc), exc_info=True)
        return False


def _has_slide_view_grant(user_id, slide_id, *, conn=None) -> bool:
    """slide_id 级显式授权（R-06：只认 slide_id 列，不回退 slide_name 匹配）。

    2026-10-08 §3.2：授权带 ``expires_at``（0080）——只认未到期行
    （``expires_at > now()``）；到期立即对新请求生效，不依赖清理任务。"""
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM slide_view_grants "
                "WHERE slide_id=%s AND user_id=%s "
                "AND expires_at > now() LIMIT 1",
                (slide_id, user_id),
            )
            return cur.fetchone() is not None


def _has_active_share_membership(user_id, slide_id, *, conn=None) -> bool:
    """share_slides ⋈ grants 成员判定：已领取未撤销未过期且含 view 权限。"""
    return _share_membership_permission(user_id, slide_id,
                                        permission="view", conn=conn)


def _share_membership_permission(user_id, slide_id, *, permission="view",
                                 conn=None) -> bool:
    """share_slides ⋈ grants 成员判定（可指定 view/annotate/download 权限档）。

    permission 是服务端白名单词（view/annotate/download），直接内嵌 JSONB
    字面量（psycopg 对 '%s'::jsonb 占位无法推断类型）。
    """
    if permission not in ("view", "annotate", "download"):
        permission = "view"
    sql = (
        "SELECT 1 FROM grants g "
        "JOIN shares sh ON sh.token = g.token "
        "JOIN share_slides ss ON ss.token = sh.token "
        "WHERE g.user_id=%s AND g.active "
        "AND ss.slide_id=%s "
        "AND sh.revoked = FALSE "
        "AND (sh.expires_at IS NULL OR sh.expires_at > now()) "
        "AND g.permissions @> '[\"" + permission + "\"]'::jsonb "
        "LIMIT 1"
    )
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(sql, (user_id, slide_id))
            return cur.fetchone() is not None


def has_share_permission(user_id, slide_id, permission="view", *, conn=None):
    """主体经 share 成员关系对 slide_id 持指定权限档（P2 扩展，annotate 闸用）。"""
    if not user_id or not slide_id:
        return False
    try:
        return _share_membership_permission(user_id, slide_id,
                                            permission=permission, conn=conn)
    except Exception:  # noqa: BLE001 - fail-closed
        _LOG.warning("share 成员权限查询失败（fail-closed）：%s",
                     slide_id, exc_info=True)
        return False


def visible_ready_slide_ids(actor_user_id=None, actor_role=None, *,
                            allow_public=True, allow_share=True,
                            conn=None) -> set:
    """主体的 ready 可读 slide_id 集合（P2 列表收敛：常量次查询）。

    与逐行 ``authorize_read`` 完全同一判定序（判定优先级见其 docstring；
    P6 运行时退役：ready 且 legacy 布局的行不在集合内——待迁移/隔离不可见），
    但以两次集合查询 + 一次 ready 行扫描在内存完成（P1-B2 遗留的列表 N+1
    收敛——合同 §3.5）。DB 异常按空集处理（fail-closed，不回退扫描）。

    - admin 角色（actor_role=ROLE_ADMIN）→ 全部 ready；
    - owner（actor_user_id == 行 owner）；
    - public（allow_public 时）；
    - slide_view_grants（slide_id 级，只认 ID 列）；
    - share_slides ⋈ grants（view 权限档；allow_share 时）。

    demo capability 通道不在本集合语义内（allowlist 逐行走 authorize_read）。
    """
    try:
        with _session(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT slide_id, owner_user_id, public "
                            "FROM slides WHERE asset_state=%s "
                            "AND storage_layout=%s",
                            (SlideState.READY, StorageLayout.ID_BUNDLE))
                rows = cur.fetchall()
                if actor_role == ROLE_ADMIN:
                    return {r["slide_id"] for r in rows}
                uid = (actor_user_id or "").strip() or None
                if not uid:
                    return set()
                granted = set()
                with c.cursor() as cur2:
                    cur2.execute("SELECT slide_id FROM slide_view_grants "
                                 "WHERE user_id=%s AND expires_at > now()",
                                 (uid,))
                    granted.update(r["slide_id"] for r in cur2.fetchall())
                if allow_share:
                    with c.cursor() as cur3:
                        cur3.execute(
                            "SELECT DISTINCT ss.slide_id FROM grants g "
                            "JOIN shares sh ON sh.token = g.token "
                            "JOIN share_slides ss ON ss.token = sh.token "
                            "WHERE g.user_id=%s AND g.active "
                            "AND sh.revoked = FALSE "
                            "AND (sh.expires_at IS NULL "
                            "     OR sh.expires_at > now()) "
                            "AND g.permissions @> '[\"view\"]'::jsonb",
                            (uid,))
                        granted.update(r["slide_id"] for r in cur3.fetchall())
                return {r["slide_id"] for r in rows
                        if (r["owner_user_id"] and uid == r["owner_user_id"])
                        or (allow_public and r["public"])
                        or r["slide_id"] in granted}
    except Exception:  # noqa: BLE001 - fail-closed 空集
        _LOG.warning("visible_ready_slide_ids 查询失败（fail-closed 空集）",
                     exc_info=True)
        return set()


def _in_demo_catalog(slide_id, *, conn=None) -> bool:
    """demo_catalog allowlist 命中（R-10；capability 通道的库层校验）。"""
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute("SELECT 1 FROM demo_catalog WHERE slide_id=%s LIMIT 1",
                        (slide_id,))
            return cur.fetchone() is not None


# --------------------------------------------------------------------------- #
# 状态迁移原语（合同 §3.1/§4：全部 expected_state 谓词 SQL CAS）
# --------------------------------------------------------------------------- #
def _cas_state(slide_id, expected_state, new_state, assignments="",
               assignment_params=(), *, conn=None) -> bool:
    """真实 CAS：UPDATE ... WHERE slide_id=%s AND asset_state=%s。

    assignments 是额外 SET 片段（如 accounted_bytes=%s），assignment_params
    是其占位参数。返回是否迁移成功（rowcount==1）；expected_state 不匹配
    即失败，不猜当前状态。
    """
    if expected_state not in SlideState.ALL:
        raise ValueError("未知 expected_state：%r" % (expected_state,))
    sql = ("UPDATE slides SET asset_state=%s, updated_at=now()"
           + ((", " + assignments) if assignments else "")
           + " WHERE slide_id=%s AND asset_state=%s")
    params = [new_state, *assignment_params, slide_id, expected_state]
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount == 1


def mark_ready(slide_id, *, accounted_bytes=None,
               expected_state=SlideState.STAGING, conn=None) -> bool:
    """→ ready（合同 §4：publish 成功 / legacy 验证通过）。

    - 新资产：publish 编排在短事务内调用（expected_state=STAGING），并把
      ``accounted_bytes`` 与 ready 同事务设置（R-12：删除结算用它）。
    - legacy 回填：scripts/backfill_slide_asset_state.py 以
      expected_state=LEGACY 调用（layout 保持 legacy，P6 再迁 id_bundle）。
    一次性 CAS：staging/legacy 之外的状态（含重复 publish）返回 False。
    """
    if accounted_bytes is not None and int(accounted_bytes) < 0:
        raise ValueError("accounted_bytes 不能为负")
    assignments = "published_at=now()"
    params = []
    if accounted_bytes is not None:
        assignments += ", accounted_bytes=%s"
        params.append(int(accounted_bytes))
    return _cas_state(slide_id, expected_state, SlideState.READY,
                      assignments, params, conn=conn)


def request_delete(slide_id, *, expected_state=SlideState.READY, conn=None,
                   requested_by=None, enqueue_job=True) -> bool:
    """ready → deleting：立即拒绝后续新读取授权（合同 §4）；P5 起与
    ``slide_delete_jobs`` 落库**同一事务**（P5 合同 §1.1——任务是删除的
    唯一执行载体，物理清理由执行器完成）。

    expected_state 谓词由调用方按行当前态给出（id_bundle 资产恒 READY；
    legacy 布局资产的统一编排允许 legacy/failed 态进入 deleting——迁移期
    清理语义，见 app._slide_delete_unified）。CAS 成功才落任务行
    （upsert：slide_id UNIQUE 防重复任务；已 done 的任务行不动）。重复
    DELETE 在 app 层分流：deleted → 幂等 200；deleting → 触发执行器
    （enqueue_delete_job 只复位 failed 任务）。
    """
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE slides SET asset_state=%s, updated_at=now() "
                "WHERE slide_id=%s AND asset_state=%s",
                (SlideState.DELETING, slide_id, expected_state))
            moved = cur.rowcount == 1
            if moved and enqueue_job:
                _upsert_delete_job(cur, slide_id, requested_by)
            return moved


def mark_deleted(slide_id, *, expected_state=SlideState.DELETING, conn=None) -> bool:
    """deleting → deleted：物理清理+结算完成后由删除 worker 调用。

    tombstone 行保留（含 legacy_filename，阻止旧别名重绑新 ID）；结算
    （按 accounted_bytes 幂等减少 used_bytes）在删除编排事务内完成（P5）。
    """
    return _cas_state(slide_id, expected_state, SlideState.DELETED,
                      "deleted_at=now()", conn=conn)


def mark_failed(slide_id, *, expected_state=SlideState.STAGING, conn=None) -> bool:
    """→ failed：staging 任务失败/取消，或 legacy 验证失败（合同 §4）。

    failed 保留证据、不可读；staging 残留文件的清理由任务侧负责
    （行可删或留 failed 证据）。
    """
    return _cas_state(slide_id, expected_state, SlideState.FAILED,
                      conn=conn)


class LayoutBindConflict(RuntimeError):
    """legacy→id_bundle 布局翻转 CAS 的证据冲突（P6 物理迁移 bound 步）。

    行状态/storage_relpath/accounted_bytes 与翻转参数不一致——不猜不绑，
    调用方（迁移工具）按冲突中止该迁移项，绝不自动修正。"""


def bind_id_bundle_layout(slide_id, storage_relpath, *, accounted_bytes=None,
                          expected_state=SlideState.READY,
                          expected_layout=StorageLayout.LEGACY,
                          conn=None) -> str:
    """legacy→id_bundle 布局翻转 CAS（P6 合同 §1.2 bound 步；runbook §4.4）。

    物理包已 no-clobber 发布到 ``objects/<slide_id>/`` 后，由本原语在短事务
    内完成 DB 侧绑定：``storage_layout→id_bundle``、``storage_relpath→新位``、
    ``accounted_bytes`` 校准（None=保持现值）。**不重复计上传配额**（不动
    used_bytes——R-12 过渡口径：历史资产已在账本内或无账本责任）；**授权
    映射零变更**（授权只能来自已审核计划，不因文件搬迁新增权限）。

    语义（带 expected 谓词的真实 SQL CAS，与 mark_* 同口径）：
      - 返回 ``"migrated"``：CAS 命中（legacy→id_bundle 一次性翻转）；
      - 返回 ``"already"``：行已处于**同参** id_bundle（崩溃恢复幂等重入——
        journal+manifest 证明同一迁移项后的重复绑定）；
      - 抛 :class:`LayoutBindConflict`：行缺失 / 布局不是 expected / 状态
        漂移 / relpath 或 accounted_bytes 与既有绑定不一致——fail-closed。

    锁序（合同 §5）：调用方事务内先 ``acquire_slide_lock``（advisory 第一把
    锁），本函数的 UPDATE 取 slides 行锁（迁移路径无任务行锁）。asset_state
    不迁移（ready 保持 ready——物理搬迁不改变可见性语义）。
    """
    rel = slide_storage.check_relpath(storage_relpath, what="storage_relpath")
    # 与 slide_storage._OBJECTS_DIRNAME 同源的字面（"objects" 是合同级布局词，
    # 见 slide_storage 模块 docstring 的磁盘布局图）。
    if rel.split("/")[:2] != ["objects", slide_id]:
        raise ValueError(
            "id_bundle storage_relpath 必须位于 objects/<本 slide_id>/ 之下：%r"
            % storage_relpath)
    if accounted_bytes is not None and int(accounted_bytes) < 0:
        raise ValueError("accounted_bytes 不能为负")
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE slides SET storage_layout=%s, storage_relpath=%s, "
                "accounted_bytes=COALESCE(%s, accounted_bytes), "
                "updated_at=now() "
                "WHERE slide_id=%s AND storage_layout=%s AND asset_state=%s",
                (StorageLayout.ID_BUNDLE, rel,
                 int(accounted_bytes) if accounted_bytes is not None else None,
                 slide_id, expected_layout, expected_state))
            if cur.rowcount == 1:
                return "migrated"
            cur.execute(
                "SELECT storage_layout, asset_state, storage_relpath, "
                "accounted_bytes FROM slides WHERE slide_id=%s", (slide_id,))
            row = cur.fetchone()
            if (row is not None
                    and row["storage_layout"] == StorageLayout.ID_BUNDLE
                    and row["asset_state"] == expected_state
                    and row["storage_relpath"] == rel
                    and (accounted_bytes is None
                         or (row["accounted_bytes"] is not None
                             and int(row["accounted_bytes"])
                             == int(accounted_bytes)))):
                return "already"
            raise LayoutBindConflict(
                "布局翻转 CAS 未命中且非同参幂等重入（slide_id=%s 行=%r "
                "期望 layout=%s state=%s relpath=%r accounted=%r）——不猜不绑"
                % (slide_id,
                   dict(row) if row is not None else None,
                   expected_layout, expected_state, rel, accounted_bytes))


def force_fail(slide_id, *, conn=None) -> bool:
    """→ failed：staging/ready 强制撤回（上传预占失效的整体撤回场景——
    内容从未入账，不得保持可见；P4-app review 补的迁移原语）。

    与 ``mark_failed``（仅 staging）的差异：本原语接受 ready——已发布
    但配额从未结算成功的资产必须能撤下（ZIP 批量中途预占失效、转换产物
    在上传任务失败后连带撤回）。ready 撤回的配额退款由调用方在同一事务
    内按 accounted_bytes 处理（未结算场景无退款）。其余状态 no-op 返回
    False（幂等，不猜不迁移 deleting/deleted）。
    """
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE slides SET asset_state=%s, updated_at=now() "
                "WHERE slide_id=%s AND asset_state IN (%s,%s)",
                (SlideState.FAILED, slide_id,
                 SlideState.STAGING, SlideState.READY))
            return cur.rowcount == 1


# --------------------------------------------------------------------------- #
# 元数据编辑（合同 §3.1：只动元数据，不动文件/legacy_filename/授权）
# --------------------------------------------------------------------------- #
def _update_meta_column(slide_id, column, value, *, conn=None) -> bool:
    """UPDATE 单列元数据；返回行是否存在（rowcount==1）。不改 asset_state
    语义、不触碰 legacy_filename/授权/存储。"""
    sql = ("UPDATE slides SET %s=%%s, updated_at=now() WHERE slide_id=%%s"
           % column)
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(sql, (value, slide_id))
            return cur.rowcount == 1


def update_display_name(slide_id, display_name, *, conn=None) -> bool:
    """改显示名（R-02：display_name 是唯一可编辑展示名）。

    不动文件、不动 legacy_filename、不动授权、不触发内容 revision 变化
    （改名不串片）。行缺失返回 False。
    """
    return _update_meta_column(slide_id, "display_name",
                               _clean_display_name(display_name), conn=conn)


def update_note(slide_id, note, *, conn=None) -> bool:
    """改备注：只动元数据。行缺失返回 False。"""
    return _update_meta_column(slide_id, "note", _clean_note(note), conn=conn)


def set_public(slide_id, public, *, conn=None) -> bool:
    """切换 public 可见位：只动元数据（授权关系不受影响）。行缺失返回 False。"""
    return _update_meta_column(slide_id, "public", bool(public), conn=conn)


# --------------------------------------------------------------------------- #
# 手动物理尺度补录（P0-W6c，0094 slide_mpp_overrides）
# --------------------------------------------------------------------------- #
def get_mpp_override(slide_id, *, conn=None):
    """读手补 mpp 行 → {"mpp_x": …, "mpp_y": …} | None（无行/任一轴有值）。

    表约束保证至少一轴非 NULL（全 None 由 set_mpp_override 删行表达）；
    读端经 apply_mpp_override 以本行为最高优先级覆盖文件推导值。
    """
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT mpp_x, mpp_y FROM slide_mpp_overrides "
                "WHERE slide_id=%s", (slide_id,))
            row = cur.fetchone()
    if row is None:
        return None
    return {"mpp_x": row["mpp_x"], "mpp_y": row["mpp_y"]}


def set_mpp_override(slide_id, mpp_x, mpp_y, updated_by, *, conn=None):
    """补录/清除手补 mpp（来源恒 manual）。

    - 任一轴非 None → UPSERT（None 轴保持库内现值）；返回新行。
    - 两轴全 None → DELETE（清除补录，读端回落文件推导）；返回 None。
    行缺失（slides 无此 slide_id）抛 ValueError。
    """
    sid = (slide_id or "").strip()
    if not sid:
        raise ValueError("slide_id 不能为空")
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute("SELECT 1 FROM slides WHERE slide_id=%s", (sid,))
            if cur.fetchone() is None:
                raise ValueError("切片不存在：%s" % sid)
            if mpp_x is None and mpp_y is None:
                cur.execute(
                    "DELETE FROM slide_mpp_overrides WHERE slide_id=%s", (sid,))
                return None
            cur.execute(
                "INSERT INTO slide_mpp_overrides (slide_id, mpp_x, mpp_y, "
                "updated_by, updated_at) VALUES (%s,%s,%s,%s,now()) "
                "ON CONFLICT (slide_id) DO UPDATE SET "
                "mpp_x=COALESCE(EXCLUDED.mpp_x, "
                "slide_mpp_overrides.mpp_x), "
                "mpp_y=COALESCE(EXCLUDED.mpp_y, "
                "slide_mpp_overrides.mpp_y), "
                "updated_by=EXCLUDED.updated_by, updated_at=now()",
                (sid, mpp_x, mpp_y, (updated_by or "").strip() or "unknown"))
            cur.execute(
                "SELECT mpp_x, mpp_y FROM slide_mpp_overrides "
                "WHERE slide_id=%s", (sid,))
            row = cur.fetchone()
    return {"mpp_x": row["mpp_x"], "mpp_y": row["mpp_y"]}


def apply_mpp_override(meta, slide_id):
    """读端覆盖层（app._read_metadata / share_server._read_metadata 共用）：

    有手补行 → 对应轴覆盖为 manual 值、mpp_source="manual"（只覆盖已补录
    的轴，另一轴保持文件推导值）；无行原样返回。这就是 mpp 解析的顶层
    优先级：manual > metadata > tiff-resolution > estimated > missing。
    """
    if not slide_id:
        return meta
    try:
        ov = get_mpp_override(slide_id)
    except Exception:  # noqa: BLE001 - override 读失败不阻断元数据读取
        return meta
    if not ov:
        return meta
    out = dict(meta)
    if ov["mpp_x"] is not None:
        out["mpp_x"] = ov["mpp_x"]
    if ov["mpp_y"] is not None:
        out["mpp_y"] = ov["mpp_y"]
    out["mpp_source"] = "manual"
    return out


# --------------------------------------------------------------------------- #
# 删除编排辅助（P3 合同 §5 最小正确版；P5 再统一两阶段）
#
# id_bundle 资产没有 legacy_filename，旧按名清理函数（share_store 的
# revoke_slide_view_grants_for_slide 等）不可用——此处提供按 slide_id 的
# 授权/关系清理原语，供删除端点在物理清理前调用（兼容调用见 app 侧：legacy
# 布局资产仍走旧按名路径）。全部可传 conn 以并入删除结算事务。
# --------------------------------------------------------------------------- #
def revoke_view_grants_by_slide_id(slide_id, *, conn=None) -> int:
    """按 slide_id 删除全部 slide_view_grants 行（R-06/R-07 失效语义）。

    新授权行恒带 slide_id（admin grant 写当前代）；NULL-ID 行是历史 unresolved
    快照，不属于本资产授权面，不动。返回删除行数。
    """
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM slide_view_grants WHERE slide_id=%s",
                        (slide_id,))
            return cur.rowcount


def remove_share_membership(slide_id, *, conn=None) -> int:
    """删除 share_slides 中该资产的成员行（R-04：原文件删除后旧 token 不因
    同名新上传恢复访问——成员行删除后授权判定（share_slides ⋈ grants）自然
    拒绝）。返回删除行数。"""
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM share_slides WHERE slide_id=%s",
                        (slide_id,))
            return cur.rowcount


def record_revision(slide_id, legacy_revision, *, conn=None) -> bool:
    """写一行 slide_assets（P3 合同 §4：id_bundle 资产的内容 revision 来源）。

    新发布写 ``legacy_revision="sha256:<hex 前缀>"``；descriptor.revision 由
    _DESCRIPTOR_SQL 取最新行（P1-A 已就位）。可传 conn 并入发布收口短事务
    （与 mark_ready/finish 同事务）。旧行 mtime:size 口径不重写。
    """
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO slide_assets (asset_id, slide_id, legacy_revision) "
                "VALUES (%s, %s, %s)",
                ("ast_" + secrets.token_urlsafe(9), slide_id,
                 str(legacy_revision)),)
            return True


# --------------------------------------------------------------------------- #
# 删除任务持久化（P5 合同 §1.1/§1.2/§2；表=slice 0067 + 0070 lease 列）
#
# slide_delete_jobs 是删除的唯一执行载体：request_delete 的 CAS 与任务落库
# 同一事务；执行器（app daemon + 端点同步尝试）经 lease 领取，退避重试。
# 重复执行/worker 重启不得重复减账——结算幂等键 = deleting→deleted 的 CAS
# 本身（减账与 CAS 同事务），与租约无关；租约只防「同一任务被两个 worker
# 同时领取」（claim 的 UPDATE 谓词 + FOR UPDATE SKIP LOCKED）。
# --------------------------------------------------------------------------- #
#: 任务状态（0067 CHECK 词表）
DELETE_JOB_PENDING = "pending"
DELETE_JOB_CLEANING = "cleaning"
DELETE_JOB_DONE = "done"
DELETE_JOB_FAILED = "failed"


def _upsert_delete_job(cur, slide_id, requested_by):
    """任务行 upsert（调用方事务内；P5 review 收口：requeue_failed 装饰形参
    已清——SQL 的 ON CONFLICT WHERE state='failed' 即正确语义，两调用点
    行为本就一致）。缺行 → 插入 pending；已有行仅 state='failed' 时复位
    pending（清 lease/错误）——pending/cleaning（含活租约）不动，done 永不动。"""
    cur.execute(
        "INSERT INTO slide_delete_jobs (job_id, slide_id, requested_by, "
        "state) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (slide_id) DO UPDATE SET "
        "state=%s, requested_by=EXCLUDED.requested_by, "
        "lease_owner=NULL, lease_expires_at=NULL, last_error=NULL, "
        "updated_at=now() WHERE slide_delete_jobs.state=%s",
        ("sdj_" + secrets.token_urlsafe(9), slide_id,
         (requested_by or None), DELETE_JOB_PENDING,
         DELETE_JOB_PENDING, DELETE_JOB_FAILED))


def enqueue_delete_job(slide_id, requested_by=None, *, conn=None) -> None:
    """为已处 deleting 的资产（重放 DELETE/执行器重启后）确保任务在队列。

    只复位 failed 任务（重试入口）；pending/cleaning/done 均为 no-op——
    活租约不被端点重放打断，done 不复活。幂等。
    """
    with _session(conn) as c:
        with c.cursor() as cur:
            _upsert_delete_job(cur, slide_id, requested_by)


def claim_due_delete_job(worker_id, *, lease_seconds=300.0,
                         backoff_base_seconds=2.0, backoff_cap_seconds=3600.0,
                         conn=None):
    """领取一条到期任务（执行器入口；lease + 退避）。

    到期谓词：
      - pending → 立即到期；
      - failed → updated_at + min(cap, base*2^attempts) <= now（指数退避）；
      - cleaning → lease_expires_at <= now（worker 崩溃恢复重领）。

    并发安全：候选行 ``FOR UPDATE SKIP LOCKED``——两实例同时领取时各自
    锁定不同行，同一行只被一个事务UPDATE 为 cleaning（第二实例跳过）；
    结算侧再由 deleting→deleted CAS 兜底（即使极端竞态下重复执行清理，
    减账与物理清理仍只发生一次语义：remove_bundle 幂等、CAS 只成功一次）。
    领取即 attempts+1 并写 lease_owner/lease_expires_at。无到期任务返回
    None。
    """
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT job_id, slide_id, state, attempts, "
                "extract(epoch from updated_at)::float8 AS updated_ts, "
                "extract(epoch from lease_expires_at)::float8 AS lease_ts "
                "FROM slide_delete_jobs "
                "WHERE state IN (%s,%s,%s) "
                "ORDER BY created_at LIMIT 5 FOR UPDATE SKIP LOCKED",
                (DELETE_JOB_PENDING, DELETE_JOB_FAILED, DELETE_JOB_CLEANING))
            rows = cur.fetchall()
            now = time.time()
            picked = None
            for r in rows:
                if r["state"] == DELETE_JOB_PENDING:
                    picked = r
                    break
                if r["state"] == DELETE_JOB_FAILED:
                    backoff = min(backoff_cap_seconds,
                                  backoff_base_seconds * (2 ** int(r["attempts"])))
                    if now >= float(r["updated_ts"] or 0) + backoff:
                        picked = r
                        break
                elif r["state"] == DELETE_JOB_CLEANING:
                    if r["lease_ts"] is None or now >= float(r["lease_ts"]):
                        picked = r
                        break
            if picked is None:
                return None
            cur.execute(
                "UPDATE slide_delete_jobs SET state=%s, attempts=attempts+1, "
                "lease_owner=%s, lease_expires_at=now() + (%s || ' seconds')::"
                "interval, last_error=NULL, updated_at=now() "
                "WHERE job_id=%s RETURNING job_id, slide_id, requested_by, "
                "attempts",
                (DELETE_JOB_CLEANING, str(worker_id or "worker"),
                 str(float(lease_seconds)), picked["job_id"]))
            row = cur.fetchone()
            return dict(row) if row else None


def finish_delete_job(job_id, state, *, error=None, conn=None) -> bool:
    """任务收口：done（结算完成）/ failed（本次尝试失败，退避后重试）。

    两者都清 lease；failed 记 last_error（截断 4KB）。done 只能由结算
    成功方写入。返回行是否存在。
    """
    if state not in (DELETE_JOB_DONE, DELETE_JOB_FAILED):
        raise ValueError("未知删除任务终态：%r" % (state,))
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE slide_delete_jobs SET state=%s, last_error=%s, "
                "lease_owner=NULL, lease_expires_at=NULL, updated_at=now() "
                "WHERE job_id=%s",
                (state, (str(error)[:4096] if error else None), job_id))
            return cur.rowcount == 1


def finish_delete_job_by_slide(slide_id, state, *, error=None, conn=None) -> bool:
    """按 slide_id 收口任务（端点同步执行成功后把 pending 任务直接置 done，
    免 daemon 空领一轮——executor 对已 deleted 幂等，晚到收口无害）。"""
    if state not in (DELETE_JOB_DONE, DELETE_JOB_FAILED):
        raise ValueError("未知删除任务终态：%r" % (state,))
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE slide_delete_jobs SET state=%s, last_error=%s, "
                "lease_owner=NULL, lease_expires_at=NULL, updated_at=now() "
                "WHERE slide_id=%s",
                (state, (str(error)[:4096] if error else None), slide_id))
            return cur.rowcount == 1


def get_delete_job(slide_id, *, conn=None):
    """按 slide_id 取任务行（None=无任务）；测试/管理面观测用。"""
    if not isinstance(slide_id, str) or not slide_id.strip():
        return None
    with _session(conn) as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT job_id, slide_id, requested_by, state, attempts, "
                "last_error, lease_owner, "
                "extract(epoch from lease_expires_at)::float8 "
                "  AS lease_expires_ts, "
                "extract(epoch from created_at)::float8 AS created_ts, "
                "extract(epoch from updated_at)::float8 AS updated_ts "
                "FROM slide_delete_jobs WHERE slide_id=%s",
                (slide_id.strip(),))
            row = cur.fetchone()
            return dict(row) if row else None


# --------------------------------------------------------------------------- #
# 跨进程仲裁锁（合同 §5 的第一把锁；publish/delete/recovery 共用）
# --------------------------------------------------------------------------- #
def acquire_slide_lock(cur, slide_id):
    """事务级 advisory 锁：``pg_advisory_xact_lock(hashtext('slide:' || slide_id))``。

    所有 publish/delete/recovery 的**第一把锁**（跨进程仲裁点）。'slide:'
    前缀与既有 ``hashtext(job_id)`` 键空间隔离；随事务结束自动释放。
    必须在取任务行锁/slides 行锁之前调用（锁序见模块 docstring）。
    cur 是已开启事务的 cursor（调用方管理事务边界）。
    """
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('slide:' || %s))",
                (slide_id,))
