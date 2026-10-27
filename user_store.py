# -*- coding: utf-8 -*-
"""用户存储层（PostgreSQL 唯一后端）。

调用方一律 ``import user_store``。``STORAGE_BACKEND`` 仅接受 ``postgres``
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
    "USER_FILE",
    "ROLE_OWNER",
    "ROLE_USER",
    "ROLE_GUEST",
    "ROLE_SDK",
    "VALID_ROLES",
    "UserStoreCorrupt",
    "OwnerInvariantError",
    "PasswordChangeConflict",
    # 统一密码策略（两实现一致）
    "PASSWORD_MIN_LENGTH",
    "PASSWORD_MAX_LENGTH",
    # user ai_config.max_steps 默认值（两实现一致）
    "DEFAULT_USER_MAX_STEPS",
    # —— 函数 ——
    "create_user",
    "get_user",
    "get_user_by_login_id",
    "verify_user",
    "list_users",
    "set_user_disabled",
    "record_login_success",
    "record_activity",
    "ACCOUNT_KINDS",
    "set_user_account_kind",
    "set_user_password",
    "change_own_password",
    "get_user_ai_config",
    "set_user_ai_config",
    "count_owners",
    "has_enabled_users",
    # 身份批投影（admin 费用/流水/格式申请主列；与用户列表 identity 同规则）
    "get_user_identity_map",
    "list_user_ids_matching_identity",
    # owner 解析与引导原语
    "list_enabled_owners",
    "list_owners",
    "create_bootstrap_owner",
    "resolve_primary_owner",
)

import user_store_pg as _pg

_missing = [n for n in _PUBLIC_NAMES if not hasattr(_pg, n)]
if _missing:
    raise RuntimeError("user_store_pg 缺少公共名 %s" % _missing)
_g = globals()
for _name in _PUBLIC_NAMES:
    _g[_name] = getattr(_pg, _name)


def __getattr__(name):
    raise AttributeError("module %r has no attribute %r" % (__name__, name))