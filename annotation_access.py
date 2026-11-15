# -*- coding: utf-8 -*-
"""标注可见性统一判定（工单 A / P0 数据隔离，docs §2 of
viewer-demo-collaboration-review-plan-20260919）。

核心契约：「能看切片」≠「能看标注」。个人标注默认私有，跨主体可见只能经由
``annotation_grants``（0056）显式授权；``shared=true`` 不再隐含「对同片所有
分享/用户开放」，只保留「对本条标注所在 token（若为真实分享链接）开放」的
收窄语义。

主体（subject）一律由服务端推导（session / 已验证分享+访客 / AI 运行归属），
**绝不采信客户端自报 owner**。本模块只做纯策略判定 + 薄授权查询：

  - subject_from_request()：admin app 请求上下文 → subject（lazy import app，
    避免循环依赖；share 端用 visitor_subject() 自行构造）；
  - can_read_annotation / can_write_annotation / can_comment_annotation；
  - filter_rois / filter_changes：数据层过滤入口（store 的 subject=None 分支
    不过滤——那是管理清点/存量测试语义，HTTP 层必须传 subject）；
  - author_projection：稳定作者身份（user|visitor|ai|unknown，AI 不是人）；
  - capabilities：can_edit / can_delete / can_comment 能力位。

判定规则（与 0056 迁移的存量映射一致，见 migration 注释）：
  - local_owner（本地免认证单租户态，owner 无 user_id）：全量可见可写——
    该形态不存在「其他用户」可隔离（can_view_slide 同款口径）；
  - unclaimed（无 owner 且无 visitor 的存量行）：默认不进正常列表（仅
    local_owner / 管理清点），但显式 annotation_grants 授权仍然生效——
    授权是比缺省归属更强的显式公开决定；
  - user：本人工作台标注（token=admin 且 owner_user_id=本人；AI 标注以
    provenance.created_by_user_id 归属优先）∪ 显式授权（annotation_grants）；
    切片可见/拥有关系本身不授予读取他人标注的权限；
  - visitor（分享链接 + 访客身份）：本 token 上本人的记录（含旧「无 visitor
    字段=链接级」兼容）∪ 本 token 上 shared=true 的记录（只读）∪ 授予该
    token 的记录；不知链接内容不能编辑他人私有记录；
      - ai（sidecar/plugin 读取）：绑定到运行属主时按该 user 语义判定；
    未绑定主体时拒绝（空集合），不得凭 source=ai 跨用户放行。

role=owner 的**认证**主体：读可见性与其他 user 同一业务口径（工作台不是
全量 dump；全量走管理台 inventory）；写路径保留 owner 特权（既有
can_delete_annotation / _check_annotation_owner 语义，见 app.py）。
"""
import share_shared

ADMIN_TOKEN = share_shared.ADMIN_TOKEN

#: subject kind 枚举（dict "kind" 字段取值）
KIND_LOCAL_OWNER = "local_owner"
KIND_USER = "user"
KIND_VISITOR = "visitor"
KIND_AI = "ai"
KIND_GUEST = "guest"


# --------------------------------------------------------------------------- #
# subject 构造
# --------------------------------------------------------------------------- #
def local_owner_subject():
    """本地免认证单租户态主体（owner 无 user_id）：全量读、全量写。"""
    return {"kind": KIND_LOCAL_OWNER}


def user_subject(user_id, role=""):
    """登录用户主体（owner/user 角色均走此构造；读可见性同业务口径）。"""
    return {"kind": KIND_USER, "user_id": user_id or "", "role": role or ""}


def visitor_subject(share_token, visitor_hash):
    """分享链接访客主体（visitor_hash 为落库形态：h1.* 哈希或旧明文）。"""
    return {"kind": KIND_VISITOR, "share_token": share_token or "",
            "visitor_hash": visitor_hash or ""}


def ai_subject(owner_user_id=None):
    """AI 读取主体：owner_user_id 为绑定的运行属主（可解析时），否则空。"""
    return {"kind": KIND_AI, "owner_user_id": owner_user_id or ""}


def guest_subject():
    """无身份主体：任何标注均不可见（HTTP 层通常已被 401/403 拦截）。"""
    return {"kind": KIND_GUEST}


def subject_from_request():
    """admin app（app.py）请求上下文 → subject（绝不读请求体自报 owner）。

    - 本地免认证单租户态（role=owner 且无 user_id）→ local_owner；
    - 其余有 user_id 的登录主体 → user（owner 与 user 同构造，读可见性由
      can_read_annotation 统一判定，不再按角色放行）；
    - guest / 无身份 → guest（不可见）。
    """
    import app as app_mod  # noqa: PLC0415  lazy：app.py 顶层 import 本模块
    ident = app_mod.current_identity()
    role = ident.get("role") or ""
    uid = ident.get("user_id") or ""
    if role == app_mod.user_store.ROLE_OWNER and not uid:
        return local_owner_subject()
    if uid:
        return user_subject(uid, role)
    return guest_subject()


# --------------------------------------------------------------------------- #
# 授权上下文（access_context）
# --------------------------------------------------------------------------- #
def access_context_for(subject):
    """为主体预取授权表 {annotation_id: can_edit}（一次查询，过滤复用）。

    查询失败 fail-closed 返回空表（宁可漏可见不可多可见；调用方仍可经
    ensure_grants 直查）。subject 无 grantee 语义（local_owner/ai 无属主/
    guest）时返回 {"grants": {}}。
    """
    grants = {}
    try:
        import share_store  # noqa: PLC0415  lazy：避免 store 侧顶层循环
        kind, gid = _grantee_of(subject)
        if kind:
            grants = share_store.annotation_grants_for_subject(kind, gid) or {}
    except Exception:  # noqa: BLE001 - 授权查询失败按无授权处理（fail-closed）
        grants = {}
    return {"grants": grants}


def _grantee_of(subject):
    """subject → (grantee_kind, grantee_id)；无授权语义返回 (None, None)。"""
    kind = (subject or {}).get("kind")
    if kind == KIND_USER:
        return "user", subject.get("user_id") or ""
    if kind == KIND_VISITOR:
        return "share_token", subject.get("share_token") or ""
    return None, None


def _granted_edit(subject, annotation_id, access_context):
    """主体是否对该标注持有 can_edit 授权（context 优先，缺省直查一次）。"""
    ctx = access_context
    if ctx is None or "grants" not in ctx:
        ctx = access_context_for(subject)
    return bool(ctx.get("grants", {}).get(annotation_id))


def _granted_read(subject, annotation_id, access_context):
    ctx = access_context
    if ctx is None or "grants" not in ctx:
        ctx = access_context_for(subject)
    return annotation_id in ctx.get("grants", {})


# --------------------------------------------------------------------------- #
# 判定核心
# --------------------------------------------------------------------------- #
def is_unclaimed(roi):
    """无 owner 且无 visitor 的存量行 → unclaimed（仅 local_owner/管理清点）。"""
    return not roi.get("owner_user_id") and not (roi.get("visitor") or "")


def _ai_attributed_user(roi):
    """AI 标注的权威属主（provenance.created_by_user_id）；无溯源返回 None。"""
    if roi.get("source") != "ai":
        return None
    prov = roi.get("provenance")
    if not isinstance(prov, dict):
        return None
    uid = prov.get("created_by_user_id")
    return uid or None


def is_ai_generated(roi):
    """真·AI 产出判定（供 ai 主体读取与作者口径使用）。

    写路径会把「工作台未公开标注」推断为 source=ai（add_roi 的历史语义），
    仅凭 source 无法区分真 AI 与人工工作台标注。真 AI 产出一律带会话/溯源
    （internal/plugin annotate 恒写 created_by_session_id 或 provenance）；
    两者皆缺的 source=ai 行按人工私有处理（fail-closed：无属主的 AI 读取
    主体不得把它带进上下文；作者口径也不把人算成 AI）。
    """
    if roi.get("source") != "ai":
        return False
    return bool(roi.get("created_by_session_id")
                or isinstance(roi.get("provenance"), dict))


def _owned_by_user(uid, roi):
    """user 主体对标注的创建者归属（AI 溯源优先于落库缺省 owner）。"""
    if not uid:
        return False
    ai_user = _ai_attributed_user(roi)
    if ai_user is not None:
        return ai_user == uid
    # 工作台写入（token=admin）以 owner_user_id 为准；分享 token 上的
    # owner_user_id 只是启动注入的缺省归属，不代表人类作者（visitor 才是）。
    if roi.get("token") == ADMIN_TOKEN:
        return roi.get("owner_user_id") == uid
    return False


def can_read_annotation(subject, roi, access_context=None):
    """读取可见性判定（纯策略；授权经 access_context 或懒查询）。

    判定顺序：local_owner 全量 → **显式授权优先**（annotation_grants 是唯一
    跨主体原语，对 unclaimed 行同样生效——授权即显式公开决定）→ unclaimed
    排除（无归属存量行不进正常列表，仅管理清点）→ 各主体默认规则。
    """
    subject = subject or {}
    kind = subject.get("kind")
    if kind == KIND_LOCAL_OWNER:
        return True
    if kind in (KIND_GUEST, None):
        return False
    aid = roi.get("annotation_id")
    if aid and _granted_read(subject, aid, access_context):
        return True
    if is_unclaimed(roi):
        return False
    if kind == KIND_USER:
        return _owned_by_user(subject.get("user_id"), roi)
    if kind == KIND_VISITOR:
        if roi.get("token") != subject.get("share_token"):
            return False
        vis = roi.get("visitor") or ""
        if not vis:
            # 旧「无 visitor 字段 = 链接级记录」兼容（_roi_owned_by 同语义）
            return True
        if subject.get("visitor_hash") and vis == subject.get("visitor_hash"):
            return True
        # 本 token 内 shared=true：对链接访客只读开放（不再外溢到兄弟链接）
        return bool(roi.get("shared"))
    if kind == KIND_AI:
        owner = subject.get("owner_user_id")
        session_id = subject.get("session_id") or ""
        if owner:
            # 绑定运行属主：按该 user 的业务可见性（他人私有不外泄），
            # 另含本会话自己写出的 AI 标注。
            if _owned_by_user(owner, roi):
                return True
            if session_id and roi.get("created_by_session_id") == session_id:
                return True
            return False
        # 未绑定主体：拒绝。不得凭 source=ai / 会话溯源跨用户放行。
        return False
    return False


def _visitor_owns(subject, roi):
    """访客主体对同 token 记录的归属（_roi_owned_by 同语义：旧空 visitor =
    链接级可编辑）。"""
    if roi.get("token") != subject.get("share_token"):
        return False
    vis = roi.get("visitor") or ""
    if not vis:
        return True
    return bool(subject.get("visitor_hash")) and vis == subject.get("visitor_hash")


def can_write_annotation(subject, roi, access_context=None):
    """修改/删除能力判定（编辑授权 or 创建者；只读授权不授予写）。"""
    subject = subject or {}
    kind = subject.get("kind")
    if kind == KIND_LOCAL_OWNER:
        return True
    if kind in (KIND_GUEST, KIND_AI, None):
        return False
    if kind == KIND_USER:
        # owner 角色保留既有写特权（与 _check_annotation_owner 语义一致）
        role = subject.get("role") or ""
        if role == "owner":
            return True
    aid = roi.get("annotation_id")
    if aid and _granted_edit(subject, aid, access_context):
        return True
    if kind == KIND_USER:
        return _owned_by_user(subject.get("user_id"), roi)
    if kind == KIND_VISITOR:
        return _visitor_owns(subject, roi)
    return False


def can_comment_annotation(subject, roi, access_context=None):
    """评论能力：可见即可评论（写门槛由路由按 annotate 权限另行校验）。"""
    return can_read_annotation(subject, roi, access_context)


def capabilities(subject, roi, access_context=None):
    """能力位投影（响应字段用；写判定与 can_write_annotation 同源）。"""
    can_edit = can_write_annotation(subject, roi, access_context)
    return {
        "can_edit": can_edit,
        "can_delete": can_edit,
        "can_comment": can_comment_annotation(subject, roi, access_context),
    }


# --------------------------------------------------------------------------- #
# 过滤入口（数据层先过滤，再分组/计数/投影）
# --------------------------------------------------------------------------- #
def filter_rois(subject, rois, access_context=None):
    """按主体过滤 ROI dict 列表（保持原序原样；不做投影/重排 index）。"""
    if subject is None:
        return list(rois)
    return [r for r in rois
            if can_read_annotation(subject, r, access_context)]


def can_see_access_event(subject, event):
    """授权变更事件：仅投递给被授权主体（撤销后仍可见，以便失效/重建）。"""
    if not event or event.get("type") != "access":
        return False
    kind, gid = _grantee_of(subject)
    if not kind or not gid:
        return False
    return (event.get("grantee_kind") == kind
            and event.get("grantee_id") == gid)


def filter_changes(subject, changes, access_context=None, roi_of=None):
    """按主体过滤变更事件（annotation + comment + access）。

    access 事件按被授权者投递（撤销后仍可见，不附带几何/token）。
    其余事件不泄漏不可见标注的文本/几何/身份/tombstone/评论。
    """
    if subject is None:
        return list(changes)
    out = []
    for ch in changes:
        if ch.get("type") == "access":
            if can_see_access_event(subject, ch):
                out.append(ch)
            continue
        if ch.get("type") == "comment":
            parent = (roi_of or (lambda _aid: None))(ch.get("annotation_id"))
            if parent is None or not can_read_annotation(subject, parent,
                                                         access_context):
                continue
            out.append(ch)
        else:
            if can_read_annotation(subject, ch, access_context):
                out.append(ch)
    return out


# 跨主体响应白名单：不含分享 bearer token / visitor 哈希 / 内部归属字段。
_PUBLIC_ROI_KEYS = (
    "annotation_id", "slide", "slide_id", "type", "x", "y", "w", "h",
    "side_px", "size_mm",
    "x1", "y1", "x2", "y2", "points", "label", "note", "shared", "ts", "source",
    "revision", "change_seq", "index", "review_status", "geometry_version",
    "author_key", "author_kind", "author_label_safe", "can_edit", "can_delete",
    "deleted", "deleted_at", "created_by_session_id",
    # §6.3 几何位置锁状态（旧行缺省 false，见 public_roi_view 归一）
    "geometry_locked",
    # P1-W1 类别快照（{"name","color"}；无类别行不落键）
    "classification",
)


def public_roi_view(roi, subject=None, access_token=None):
    """标注对外投影：默认去掉来源分享 token。

    token 仅在下列情况保留（调用方已持有该凭据，不是新的泄漏）：
      - token == ADMIN_TOKEN（工作台定位符，不是分享 bearer）；
      - token == 当前分享页的 access_token。
    §6.3：geometry_locked 顶层布尔；旧行无该键 → 显式补 false（缺省未锁，
    不让客户端猜缺键语义）。
    """
    if not isinstance(roi, dict):
        return {}
    out = {}
    for k in _PUBLIC_ROI_KEYS:
        if k in roi:
            out[k] = roi[k]
    if "geometry_locked" not in out and not out.get("deleted"):
        out["geometry_locked"] = False
    tok = roi.get("token")
    if tok == ADMIN_TOKEN:
        out["token"] = tok
    elif access_token and tok and tok == access_token:
        out["token"] = tok
    return out


_PUBLIC_COMMENT_KEYS = (
    "comment_id", "annotation_id", "slide", "slide_id", "body", "parent_id",
    "resolved",
    "deleted", "created_at", "updated_at", "author_label", "change_seq", "type",
    # 分享页掩码红线需要 author_user_id 区分成员/访客（不透明内部 id，
    # 非邮箱/登录名；明文身份由 share 端点替换为掩码 label 后才投影）。
    "author_user_id",
)


def public_comment_view(cmt, access_token=None):
    """评论对外投影：去掉来源分享 bearer / 内部身份。"""
    if not isinstance(cmt, dict):
        return {}
    out = {}
    for k in _PUBLIC_COMMENT_KEYS:
        if k in cmt:
            out[k] = cmt[k]
    tok = cmt.get("token")
    if tok == ADMIN_TOKEN:
        out["token"] = tok
    elif access_token and tok and tok == access_token:
        out["token"] = tok
    return out


def public_access_event_view(event):
    """授权事件对外投影：不含分享 token / 几何 / 备注。"""
    if not isinstance(event, dict):
        return {}
    return {
        "type": "access",
        "op": event.get("op"),
        "annotation_id": event.get("annotation_id"),
        "slide": event.get("slide"),
        "slide_id": event.get("slide_id"),
        "change_seq": event.get("change_seq"),
        "reset_required": event.get("op") == "revoke",
        "grantee_kind": event.get("grantee_kind"),
    }


# --------------------------------------------------------------------------- #
# 作者身份投影（people 口径：AI 不是人；未知作者不造人）
# --------------------------------------------------------------------------- #
def author_projection(roi, label_of_user=None):
    """稳定作者身份 {author_key, author_kind, author_label_safe}。

    kind ∈ user|visitor|ai|unknown：
      - 真 AI 产出（is_ai_generated）→ ai（不计入人数）；工作台未公开标注
        虽被写路径推断为 source=ai，但无会话/溯源，按其真实作者（owner/
        visitor）计，不把人算成 AI；
      - 有 visitor → visitor（分享范围内的稳定伪名，HMAC 哈希前缀去重，
        不回传完整哈希/原始 cookie）；
      - 有 owner_user_id（工作台记录）→ user（label 经 label_of_user 回调
        解析，调用方负责掩码策略；未提供回调返回 None）；
      - 其余 → unknown（不发明作者）。
    """
    if is_ai_generated(roi):
        return {"author_key": "ai", "author_kind": "ai",
                "author_label_safe": "AI"}
    vis = roi.get("visitor") or ""
    if vis:
        prefix = vis[3:15] if vis.startswith("h1.") else vis[:12]
        return {"author_key": "visitor:%s" % prefix, "author_kind": "visitor",
                "author_label_safe": None}
    owner = roi.get("owner_user_id")
    if owner:
        label = None
        if callable(label_of_user):
            try:
                label = label_of_user(owner)
            except Exception:  # noqa: BLE001 - 展示字段失败降级 None
                label = None
        return {"author_key": "user:%s" % owner, "author_kind": "user",
                "author_label_safe": label}
    return {"author_key": "unknown", "author_kind": "unknown",
            "author_label_safe": None}
