# -*- coding: utf-8 -*-
"""切片分享存储层（PostgreSQL 唯一后端）。

调用方一律 ``import share_store``。``STORAGE_BACKEND`` 仅接受 ``postgres``
（缺省即 postgres）。json/dual 实现已删除。
"""
import os as _os

_BACKEND_ENV = "STORAGE_BACKEND"
STORAGE_BACKEND = (_os.environ.get(_BACKEND_ENV) or "postgres").strip()
if STORAGE_BACKEND != "postgres":
    raise ValueError(
        "STORAGE_BACKEND 仅支持 postgres，当前值为 %r" % STORAGE_BACKEND
    )

_PUBLIC_NAMES = (
    # —— 常量 ——
    "SHARE_DATA_DIR",
    "SHARE_FILE",
    "ROI_TYPES",
    "ALLOWED_ROI_SIZES",
    "DEFAULT_ROI_SIZES",
    "ADMIN_TOKEN",
    "PERMISSION_VIEW",
    "PERMISSION_ANNOTATE",
    "PERMISSION_DOWNLOAD",
    "DEFAULT_PERMISSIONS",
    "AUDIT_MAX_EVENTS",  # 审计日志封顶条数
    # —— 异常类（CAS 与 review G1 fail-closed：损坏 / IO 不可用分流）——
    "RevisionConflict",
    "AnnotationLocked",
    "ShareStoreCorrupt",
    "ShareStoreUnavailable",
    # —— 函数 ——
    "set_owner_user_id",
    "get_owner_user_id",  # P3：无 UID 本地模式的资产 owner 解析口径
    "probe_readable",
    "create_share",
    "get_share",
    "list_shares",
    "revoke_share",
    "claim_share",
    "claimed_active_slides_for_user",
    "list_grants_for_user",
    "grant_slide_view",
    "start_slide_view_grant_timed",
    "end_slide_view_grant",
    "active_slide_view_grants_for_user",
    "revoke_slide_view_grants_for_slide",
    "slide_view_grants_for_user",
    "list_slide_view_grants",
    "add_roi",
    "update_roi",
    "list_rois",
    "get_roi",
    "get_roi_by_annotation_id",
    "rehash_plaintext_visitors",
    "delete_roi",
    "delete_roi_by_annotation_id",
    "restore_roi",
    "update_roi_by_annotation_id",
    "set_roi_geometry_locked",
    "upsert_ai_session_principal",
    "get_ai_session_principal",
    "list_changes",
    "current_change_seq",
    "set_roi_shared",
    "set_roi_shared_by_annotation_id",
    "roi_count_by_token",
    "list_shared_rois_for_slides",
    # —— 标注级授权 + 可见性（0056 工单 A / P0）——
    "grant_annotation_to_user",
    "grant_annotation_to_share",
    "revoke_grant",
    "list_grants",
    "annotation_grants_for_subject",
    "annotation_visibility_report",
    "review_roi",
    "review_roi_by_annotation_id",
    "add_comment",
    "list_comments",
    "resolve_comment",
    "delete_comment",
    "set_slide_meta",
    "get_slide_meta",
    "get_slide_meta_full",
    "get_all_slide_meta_full",
    "get_all_slide_meta",
    "get_slide_id",
    "resolve_slide_ref",
    "record_slide_asset",
    "ProjectParentError",
    "PROJECT_MAX_DEPTH",
    "PROJECT_PARENT_UNCHANGED",
    "create_project",
    "list_projects",
    "get_project",
    "update_project",
    "add_slides_to_project",
    "remove_slide_from_project",
    "remove_slide_from_project_by_id",
    "delete_project",
    "annotations_by_slide",
    "annotations_by_project",
    # —— 审计日志 / 归档只读 ——
    "record_audit",
    "list_audit",
    "set_project_archived",
    "archived_slide_names",
    # —— 插件安装凭证 + run grant ——
    "create_plugin_installation",
    "rotate_installation_secret",
    "get_plugin_installation",
    "verify_installation_secret",
    "set_installation_enabled",
    "list_plugin_installations",
    # —— 插件能力层：能力注册表登记 ——
    "set_installation_capabilities",
    "set_installation_approved_scopes",
    "create_run_grant",
    "get_run_grant",
    "revoke_run_grant",
    "list_run_grants_for_session",
    "bind_run_grant_session",
    "list_run_grants",
    # —— 描绘按需技能（0082 / viewer-drawing-skill-contract §3.2） ——
    "set_run_grant_skill_delineate",
    # —— P1-W1 标注类别引用校验注入（resolver 由 app 接线 label_class_store） ——
    "set_classification_resolver",
    # —— 会话级「允许 AI 描绘」开关镜像（0039/0041 / P1-4+三轮 P1） ——
    "get_ai_session_drawing_flag",
    "upsert_ai_session_drawing_flag",
    "init_ai_session_drawing_flag",
    "get_ai_session_drawing_generation",
    "reserve_ai_session_drawing_generation",
    "cas_ai_session_drawing_flag",
)

import share_store_pg as _pg

_missing = [n for n in _PUBLIC_NAMES if not hasattr(_pg, n)]
if _missing:
    raise RuntimeError("share_store_pg 缺少公共名 %s" % _missing)
_g = globals()
for _name in _PUBLIC_NAMES:
    _g[_name] = getattr(_pg, _name)


def __getattr__(name):
    raise AttributeError("module %r has no attribute %r" % (__name__, name))