# -*- coding: utf-8 -*-
"""LLM 视觉供应商仓储（P1-W0a，PG 存储 + Fernet 密钥注入）。

模式对齐 settings_store / baidu_import_store：模块级函数 + 中文注释，
psycopg dict_row，事务走 pg_store.transaction。

密钥处理（范式：app.py ai_secret Fernet key 文件 0600 模式）：
  - app 启动时 ``set_fernet_factory(app._load_or_create_ai_secret)`` 注入；
    本模块**不** import app（无环）。factory 返回 Fernet 或 None；
  - api_key 落库恒为 ``enc:`` + Fernet 密文；明文只在
    :func:`get_provider_for_call`（服务端调上游专用）解密返回；
  - Fernet 不可用 → 新建/换 key 抛 :class:`SecretUnavailableError`
    （fail-closed，比照官方模式门禁；0094 的 ``^enc:`` CHECK 是数据库层兜底）；
  - 公共视图（:func:`public_provider` / :func:`list_providers`）只含
    ``api_key_tail4``（写入时另算），永不回显明文或密文。

审计：owner CRUD 写 audit_events（action=llm_provider.*），detail 只含
provider_id/name 等非敏感字段。
"""
from __future__ import annotations

import json
import secrets
import time

import psycopg

import pg_store
import llm_vision

#: 注入的 Fernet factory（app 启动接线；测试直接 monkeypatch 本属性）
_fernet_factory = None

_ENC_PREFIX = "enc:"

#: 允许 owner 粘贴的 base_url 长度上限（防滥用；正常 URL 远小于此）
_MAX_BASE_URL_LEN = 2048


class StoreError(Exception):
    """基类（HTTP 层 400）。"""

class ValidationError(StoreError):
    """输入形状非法。"""

class BaseUrlInvalidError(ValidationError):
    """base_url 不合法/指向禁入目标（SSRF 防线）。"""

class SecretUnavailableError(StoreError):
    """Fernet 不可用时新增/更换 api_key（fail-closed）。"""

class NotFoundError(StoreError):
    """provider 不存在（HTTP 404）。"""

class RevisionConflictError(StoreError):
    """revision CAS 冲突（HTTP 409）。"""


def set_fernet_factory(factory):
    """app 启动接线：注入 ``() -> Fernet | None``（复用 ai_secret.key）。"""
    global _fernet_factory
    _fernet_factory = factory


def _fernet():
    return _fernet_factory() if _fernet_factory is not None else None


def _encrypt(plain: str) -> str:
    f = _fernet()
    if f is None:
        raise SecretUnavailableError(
            "密钥加密不可用（ai_secret 缺失/损坏）：暂不能保存 API key，"
            "请检查部署后重试")
    return _ENC_PREFIX + f.encrypt(plain.encode("utf-8")).decode("ascii")


def _decrypt(stored: str) -> str:
    if not stored or not stored.startswith(_ENC_PREFIX):
        return ""  # 0094 CHECK 保证不会出现明文行；空串=未配置
    f = _fernet()
    if f is None:
        return ""
    try:
        return f.decrypt(stored[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def validate_base_url(url: str) -> str:
    """llm_vision 校验 + 长度上限；异常归一为本模块异常类型。"""
    if not isinstance(url, str) or len(url) > _MAX_BASE_URL_LEN:
        raise BaseUrlInvalidError("base_url 非法")
    try:
        return llm_vision.validate_base_url(url)
    except llm_vision.BaseUrlInvalid as exc:
        raise BaseUrlInvalidError(str(exc)) from None


# --------------------------------------------------------------------------- #
# 行 ↔ dict
# --------------------------------------------------------------------------- #

def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _row_to_dict(row) -> dict:
    out = dict(row)
    out["models"] = list(out.get("models") or [])
    out["created_at"] = (out["created_at"].timestamp()
                         if out.get("created_at") else None)
    out["updated_at"] = (out["updated_at"].timestamp()
                         if out.get("updated_at") else None)
    return out


def public_provider(row: dict) -> dict:
    """owner/公共视图：绝不含 api_key 明文或密文；含尾 4 位掩码。"""
    return {
        "provider_id": row["provider_id"],
        "name": row["name"],
        "adapter": row.get("adapter", "openai_compat"),
        "base_url": row["base_url"],
        "api_key_tail4": row.get("api_key_tail4", ""),
        "api_key_set": bool(row.get("api_key_tail4")),
        "models": list(row.get("models") or []),
        "default_model": row.get("default_model", ""),
        "enabled": bool(row.get("enabled")),
        "is_default": bool(row.get("is_default")),
        "revision": int(row.get("revision", 1)),
        "updated_at": row.get("updated_at"),
    }


def user_provider_view(row: dict) -> dict:
    """普通用户最小视图（无 base_url / 无尾 4 位——上游端点信息仅 owner 可见）。

    调用方（HTTP 层）保证只传 enabled 行。
    """
    return {
        "provider_id": row["provider_id"],
        "name": row["name"],
        "models": list(row.get("models") or []),
        "default_model": row.get("default_model", ""),
        "is_default": bool(row.get("is_default")),
    }


def _audit(cur, action: str, detail: dict):
    """写 audit_events（detail 绝不含 api_key 材料；调用方保证）。"""
    cur.execute(
        "INSERT INTO audit_events (event_id, ts, actor_role, action, "
        "target_type, detail) VALUES (%s, now(), 'owner', %s, 'llm_providers', %s) "
        "ON CONFLICT (event_id) DO NOTHING",
        ("aud_llmp_" + secrets.token_hex(16), action,
         psycopg.types.json.Jsonb(detail)))


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

def _validate_models(models) -> list:
    if not isinstance(models, list) or not models:
        raise ValidationError("models 需为非空字符串数组")
    out = []
    for m in models:
        if not isinstance(m, str) or not (1 <= len(m.strip()) <= 120):
            raise ValidationError("models 需为非空字符串数组")
        out.append(m.strip())
    if len(set(out)) != len(out):
        raise ValidationError("models 不得重复")
    return out


def _validate_name(name) -> str:
    if not isinstance(name, str):
        raise ValidationError("name 非法")
    name = name.strip()
    if not (1 <= len(name) <= 80):
        raise ValidationError("name 长度需在 1-80 之间")
    return name


def create_provider(*, name, base_url, api_key, models, default_model,
                    enabled=True, is_default=False, created_by="") -> dict:
    """新建供应商（api_key 必填且加密落库）。返回公共视图 dict。"""
    name = _validate_name(name)
    base_url = validate_base_url(base_url)
    models = _validate_models(models)
    if not isinstance(api_key, str) or len(api_key.strip()) < 8:
        raise ValidationError("api_key 需为至少 8 位字符串")
    api_key = api_key.strip()
    dm = (default_model or "").strip()
    if dm and dm not in models:
        raise ValidationError("default_model 需在 models 列表内")
    encrypted = _encrypt(api_key)

    pid = "lp_" + secrets.token_hex(12)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                if is_default:
                    cur.execute("UPDATE llm_providers SET is_default=false "
                                "WHERE is_default")
                cur.execute(
                    "INSERT INTO llm_providers (provider_id, name, base_url, "
                    "api_key_encrypted, api_key_tail4, models, default_model, "
                    "enabled, is_default, created_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (pid, name, base_url, encrypted, api_key[-4:],
                     psycopg.types.json.Jsonb(models), dm or models[0],
                     bool(enabled), bool(is_default), created_by))
                row = _row_to_dict(cur.fetchone())
                _audit(cur, "llm_provider.created",
                       {"provider_id": pid, "name": name})
    except psycopg.errors.UniqueViolation as exc:
        # 0000 语义：name 唯一 / provider_id 撞（后者概率忽略）
        raise ValidationError("供应商名称已存在") from exc
    finally:
        conn.close()
    return public_provider(row)


def update_provider(provider_id: str, *, expected_revision=None, **fields) -> dict:
    """部分更新：api_key 为空串/缺省=保留旧值；带 expected_revision 时 CAS。

    base_url/models/name/default_model/enabled/is_default 可更新。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT * FROM llm_providers WHERE provider_id=%s "
                            "FOR UPDATE", (provider_id,))
                row = cur.fetchone()
                if row is None:
                    raise NotFoundError("供应商不存在")
                if expected_revision is not None \
                        and int(expected_revision) != int(row["revision"]):
                    raise RevisionConflictError("配置已被他人修改，请刷新后重试")

                sets, params = [], []
                if "name" in fields:
                    sets.append("name=%s")
                    params.append(_validate_name(fields["name"]))
                if "base_url" in fields:
                    sets.append("base_url=%s")
                    params.append(validate_base_url(fields["base_url"]))
                if "models" in fields:
                    sets.append("models=%s")
                    params.append(psycopg.types.json.Jsonb(
                        _validate_models(fields["models"])))
                if "api_key" in fields and str(fields["api_key"] or "").strip():
                    plain = str(fields["api_key"]).strip()
                    if len(plain) < 8:
                        raise ValidationError("api_key 需为至少 8 位字符串")
                    sets.append("api_key_encrypted=%s")
                    params.append(_encrypt(plain))
                    sets.append("api_key_tail4=%s")
                    params.append(plain[-4:])
                if "default_model" in fields:
                    dm = str(fields["default_model"] or "").strip()
                    models = fields.get("models")
                    if models is None:
                        cur.execute("SELECT models FROM llm_providers "
                                    "WHERE provider_id=%s", (provider_id,))
                        models = (cur.fetchone() or {}).get("models") or []
                    if dm and dm not in list(models):
                        raise ValidationError("default_model 需在 models 列表内")
                    sets.append("default_model=%s")
                    params.append(dm)
                if "enabled" in fields:
                    sets.append("enabled=%s")
                    params.append(bool(fields["enabled"]))
                if "is_default" in fields:
                    if fields["is_default"]:
                        cur.execute("UPDATE llm_providers SET is_default=false "
                                    "WHERE is_default")
                    sets.append("is_default=%s")
                    params.append(bool(fields["is_default"]))
                if not sets:
                    return public_provider(_row_to_dict(row))
                sets.append("updated_at=now()")
                sets.append("revision=revision+1")
                params.append(provider_id)
                cur.execute(
                    "UPDATE llm_providers SET " + ", ".join(sets) +
                    " WHERE provider_id=%s RETURNING *", tuple(params))
                out = _row_to_dict(cur.fetchone())
                changed = sorted(k for k in fields if k != "api_key")
                if "api_key" in fields and str(fields["api_key"] or "").strip():
                    changed.append("api_key")  # 只记“换了 key”这一事实，绝不记值
                _audit(cur, "llm_provider.updated",
                       {"provider_id": provider_id, "fields": changed})
    finally:
        conn.close()
    return public_provider(out)


def delete_provider(provider_id: str) -> None:
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("DELETE FROM llm_providers WHERE provider_id=%s "
                            "RETURNING name", (provider_id,))
                row = cur.fetchone()
                if row is None:
                    raise NotFoundError("供应商不存在")
                _audit(cur, "llm_provider.deleted",
                       {"provider_id": provider_id, "name": row["name"]})
    finally:
        conn.close()


def set_default(provider_id: str) -> None:
    """设默认（全局唯一；不存在 404）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT name FROM llm_providers WHERE provider_id=%s "
                            "FOR UPDATE", (provider_id,))
                if cur.fetchone() is None:
                    raise NotFoundError("供应商不存在")
                cur.execute("UPDATE llm_providers SET is_default=false "
                            "WHERE is_default")
                cur.execute("UPDATE llm_providers SET is_default=true, "
                            "updated_at=now(), revision=revision+1 "
                            "WHERE provider_id=%s", (provider_id,))
                _audit(cur, "llm_provider.default_set",
                       {"provider_id": provider_id})
    finally:
        conn.close()


def list_providers(*, enabled_only=False) -> list:
    """全部供应商（公共视图；按 name 排序）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM llm_providers %s ORDER BY name"
                        % ("WHERE enabled" if enabled_only else ""))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [public_provider(_row_to_dict(r)) for r in rows]


def get_provider(provider_id: str):
    """公共视图单查；不存在返回 None。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM llm_providers WHERE provider_id=%s",
                        (provider_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    return public_provider(_row_to_dict(row)) if row else None


def get_provider_for_call(provider_id: str):
    """服务端调用上游专用：含解密后的 api_key 明文（绝不进 HTTP 响应）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM llm_providers WHERE provider_id=%s",
                        (provider_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    out = _row_to_dict(row)
    out["api_key"] = _decrypt(out.pop("api_key_encrypted", ""))
    return out


def get_default_provider():
    """默认供应商（服务端视图，含明文 key）；无默认/禁用 → None（优雅降级）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM llm_providers WHERE is_default "
                        "AND enabled LIMIT 1")
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    out = _row_to_dict(row)
    out["api_key"] = _decrypt(out.pop("api_key_encrypted", ""))
    return out


def record_vision_usage(*, provider_name: str, model: str, subject_role: str,
                        subject_id: str, user_id: str, usage: dict,
                        occurred_at: float | None = None):
    """成功调用的计量：ai_usage_events 一行（status=unpriced，决策 D3）。

    owner 自配外部供应商没有价格表——priced 需要的完整计价字段留空是诚实
    口径；token 用量取上游 usage 回执（缺失全 NULL，CHECK 允许）。事件进入
    既有 admin usage-events 视图（同表读取）。
    """
    tokens = llm_vision.normalize_usage(usage)
    now = occurred_at if occurred_at is not None else time.time()
    evt = "uev_" + secrets.token_hex(16)
    call = "lvc_" + secrets.token_hex(16)
    import hashlib
    payload_hash = hashlib.sha256(
        json.dumps({"provider": provider_name, "model": model,
                    "subject": [subject_role, subject_id],
                    "usage": tokens, "ts": round(now, 3)},
                   sort_keys=True).encode("utf-8")).hexdigest()
    conn = _connect()
    try:
        with pg_store.transaction(conn) as cur:
            cur.execute(
                "INSERT INTO ai_usage_events "
                "(event_id, call_id, payload_hash, schema_version, session_id, "
                " subject_type, subject_id, user_id, provider, model, "
                " cache_hit_input_tokens, cache_miss_input_tokens, "
                " output_tokens, total_tokens, occurred_at, enqueued_at, "
                " status, unpriced_reason) "
                "VALUES (%s,%s,%s,1,'llm-vision',%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "to_timestamp(%s),to_timestamp(%s),'unpriced','llm_vision')",
                (evt, call, payload_hash, subject_role, subject_id,
                 user_id or None, provider_name, model,
                 tokens.get("cache_hit_input_tokens"),
                 tokens.get("cache_miss_input_tokens"),
                 tokens.get("output_tokens"), tokens.get("total_tokens"),
                 now, now))
    finally:
        conn.close()
    return evt
