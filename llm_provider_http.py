# -*- coding: utf-8 -*-
"""LLM 供应商 HTTP 装配层（P1-W0b，纯函数 ``(payload, status)``，无 Flask 依赖）。

app.py 只做薄路由接线（本模块不注册路由；CSRF 由全局 before_request 覆盖
cookie-session 写端点）。``ident`` 为 ``app.current_identity()`` 形态 dict
（role/user_id）。

端点（错误信封 ``{error, code, retryable?, details?}``，新模块规约 #3）：
  - GET    /api/llm/providers          owner 全量视图 / user 启用最小视图；guest 403
  - POST   /api/llm/providers          仅 owner
  - PATCH  /api/llm/providers/<id>     仅 owner（可选 revision CAS）
  - DELETE /api/llm/providers/<id>     仅 owner
  - POST   /api/llm/test               仅 owner（provider_id 或草稿三件套）
  - POST   /api/llm/vision             owner+user（guest 403；限流；计量）

安全红线：api_key 只进上游 Authorization 头；响应/日志绝不含明文或密文
（本模块不 log 请求体）；vision 代理仅服务端发起（llm_vision.call_vision，
allow_redirects=False + 请求时 DNS 复核）。
"""
from __future__ import annotations

import threading
import time

import llm_provider_store as store
import llm_vision
import user_store

#: vision 限流：每用户每分钟次数（进程内 token bucket，与插件限流同式）
_VISION_RATE_PER_MIN = 6
_VISION_BURST = 3


class _TokenBucket:
    """进程内每主体令牌桶（gunicorn 单 worker 语义与 _PLUGIN_RATE_LIMITER 一致）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}  # subject -> (tokens, last_refill)

    def _refill(self, tokens, last, per_min, burst, now):
        capacity = per_min + burst
        elapsed = max(0.0, now - last)
        tokens = min(capacity, tokens + elapsed * (per_min / 60.0))
        return tokens

    def consume(self, subject, per_min=None):
        """尝试消费 1 个令牌；成功 True，失败 (False, retry_after_seconds)。"""
        per_min = per_min if per_min is not None else _VISION_RATE_PER_MIN
        now = time.monotonic()
        with self._lock:
            tokens, last = self._state.get(subject, (per_min + _VISION_BURST, now))
            tokens = self._refill(tokens, last, per_min, _VISION_BURST, now)
            if tokens < 1.0:
                self._state[subject] = (tokens, now)
                need = 1.0 - tokens
                retry = max(1, int(need / (per_min / 60.0)) + 1)
                return False, retry
            self._state[subject] = (tokens - 1.0, now)
            return True, 0

    def reset(self):
        with self._lock:
            self._state.clear()


_RATE_LIMITER = _TokenBucket()

#: vision 请求体上限：image_base64 字符数（~6MB 像素数据的 b64 上界）
_MAX_IMAGE_B64_BYTES = 8 * 1024 * 1024
#: prompt 长度上限（字符）
_MAX_PROMPT_CHARS = 4000
#: vision 回复 max_tokens（成本护栏；上游可自行 clamp）
_VISION_MAX_TOKENS = 2048


def _err(message, code, status, *, retryable=None, details=None):
    payload = {"error": message, "code": code}
    if retryable is not None:
        payload["retryable"] = bool(retryable)
    if details is not None:
        payload["details"] = details
    return payload, status


def _is_owner(ident) -> bool:
    return ident.get("role") == user_store.ROLE_OWNER


def _logged_in(ident) -> bool:
    return ident.get("role") in (user_store.ROLE_OWNER, user_store.ROLE_USER)


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

def list_providers(ident):
    if not _logged_in(ident):
        return _err("无权访问", "forbidden", 403)
    if _is_owner(ident):
        return {"providers": store.list_providers()}, 200
    # user：启用的最小视图（供前端「AI 识图」展示可用模型）
    rows = store.list_providers(enabled_only=True)
    return {"providers": [store.user_provider_view(r) for r in rows]}, 200


def create_provider(ident, body):
    if not _is_owner(ident):
        return _err("仅管理员可配置 AI 供应商", "forbidden", 403)
    try:
        row = store.create_provider(
            name=body.get("name"), base_url=body.get("base_url"),
            api_key=body.get("api_key"), models=body.get("models"),
            default_model=body.get("default_model") or "",
            enabled=bool(body.get("enabled", True)),
            is_default=bool(body.get("is_default", False)),
            created_by=ident.get("user_id") or "")
    except store.BaseUrlInvalidError as exc:
        return _err(str(exc), "base_url_invalid", 400)
    except store.SecretUnavailableError as exc:
        return _err(str(exc), "secret_unavailable", 503, retryable=True)
    except store.ValidationError as exc:
        return _err(str(exc), "invalid_payload", 400)
    except Exception:
        return _err("保存失败，请稍后重试", "internal", 500)
    return row, 200


def update_provider(ident, provider_id, body):
    if not _is_owner(ident):
        return _err("仅管理员可配置 AI 供应商", "forbidden", 403)
    allowed = ("name", "base_url", "api_key", "models", "default_model",
               "enabled", "is_default")
    fields = {k: body[k] for k in allowed if k in body}
    if not fields and "expected_revision" not in body:
        return _err("无可更新字段", "invalid_payload", 400)
    try:
        row = store.update_provider(
            provider_id, expected_revision=body.get("expected_revision"),
            **fields)
    except store.BaseUrlInvalidError as exc:
        return _err(str(exc), "base_url_invalid", 400)
    except store.SecretUnavailableError as exc:
        return _err(str(exc), "secret_unavailable", 503, retryable=True)
    except store.RevisionConflictError as exc:
        return _err(str(exc), "revision_conflict", 409)
    except store.NotFoundError:
        return _err("供应商不存在", "not_found", 404)
    except store.ValidationError as exc:
        return _err(str(exc), "invalid_payload", 400)
    except Exception:
        return _err("更新失败，请稍后重试", "internal", 500)
    return row, 200


def delete_provider(ident, provider_id):
    if not _is_owner(ident):
        return _err("仅管理员可配置 AI 供应商", "forbidden", 403)
    try:
        store.delete_provider(provider_id)
    except store.NotFoundError:
        return _err("供应商不存在", "not_found", 404)
    except Exception:
        return _err("删除失败，请稍后重试", "internal", 500)
    return {"ok": True}, 200


# --------------------------------------------------------------------------- #
# POST /api/llm/test（连通测试：1px PNG + 短 prompt，mock 上游可测）
# --------------------------------------------------------------------------- #

def test_connection(ident, body):
    if not _is_owner(ident):
        return _err("仅管理员可测试连接", "forbidden", 403)
    provider_id = body.get("provider_id")
    if provider_id:
        provider = store.get_provider_for_call(str(provider_id))
        if provider is None:
            return _err("供应商不存在", "not_found", 404)
        api_key = provider.get("api_key") or ""
        if not api_key:
            return _err("api_key 不可用（密钥缺失/损坏），请重新录入",
                        "secret_unavailable", 400)
        model = str(body.get("model") or provider.get("default_model") or "")
        if not model:
            return _err("缺少模型名", "invalid_payload", 400)
    else:
        # 草稿三件套（保存前试连）；校验同保存路径
        try:
            base_url = store.validate_base_url(str(body.get("base_url") or ""))
        except store.BaseUrlInvalidError as exc:
            return _err(str(exc), "base_url_invalid", 400)
        api_key = str(body.get("api_key") or "").strip()
        model = str(body.get("model") or "").strip()
        if len(api_key) < 8 or not model:
            return _err("api_key/model 非法", "invalid_payload", 400)
        provider = {"adapter": "openai_compat", "base_url": base_url}
    result = llm_vision.call_vision(
        provider, api_key, model=model, prompt="ping",
        image_b64=llm_vision.TEST_IMAGE_PNG_B64, mime=llm_vision.TEST_IMAGE_MIME,
        max_tokens=16)
    out = {
        "ok": bool(result.get("ok")),
        "model": model,
        "duration_ms": result.get("duration_ms", 0),
    }
    if result.get("ok"):
        out["reply_preview"] = (result.get("text") or "")[:120]
    else:
        out["code"] = result.get("error_code") or "upstream_error"
        out["error"] = result.get("error_message") or "上游返回错误"
        out["http_status"] = result.get("status") or 0
    return out, 200


# --------------------------------------------------------------------------- #
# POST /api/llm/vision（图像+prompt 代理；限流；计量）
# --------------------------------------------------------------------------- #

def vision(ident, body):
    if not _logged_in(ident):
        return _err("无权使用 AI 识图", "forbidden", 403)

    ok, retry_after = _RATE_LIMITER.consume(
        ident.get("user_id") or ident.get("role") or "anon",
        per_min=_VISION_RATE_PER_MIN)
    if not ok:
        return ({"error": "请求过于频繁，请稍后重试（每分钟 %d 次）"
                         % _VISION_RATE_PER_MIN,
                 "code": "rate_limited", "retryable": True,
                 "retry_after": retry_after}, 429)

    image_b64 = body.get("image_base64")
    if not isinstance(image_b64, str) or not image_b64:
        return _err("缺少图像数据", "invalid_image", 400)
    if len(image_b64) > _MAX_IMAGE_B64_BYTES:
        return _err("图像数据过大，请缩小选取范围后重试", "invalid_image", 400)
    mime = str(body.get("mime") or "image/jpeg")
    if mime not in llm_vision.ALLOWED_IMAGE_MIMES:
        return _err("仅支持 JPEG/PNG 图像", "invalid_image", 400)
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not (1 <= len(prompt.strip())
                                           <= _MAX_PROMPT_CHARS):
        return _err("prompt 需为 1-%d 字符" % _MAX_PROMPT_CHARS,
                    "invalid_prompt", 400)

    # 供应商解析：指定 → 默认；未配置/禁用 → 优雅降级（不 5xx）
    provider_id = body.get("provider_id")
    if provider_id:
        provider = store.get_provider_for_call(str(provider_id))
        if provider is None:
            return _err("供应商不存在", "not_found", 404)
        if not provider.get("enabled"):
            return _err("该供应商已被停用", "not_configured", 400)
    else:
        provider = store.get_default_provider()
        if provider is None:
            return _err("AI 识图未配置或已停用，请联系管理员在设置中配置供应商",
                        "not_configured", 400)
    if not provider.get("api_key"):
        return _err("api_key 不可用（密钥缺失/损坏），请联系管理员重新录入",
                    "secret_unavailable", 400)

    model = str(body.get("model") or provider.get("default_model") or "")
    if model and model not in (provider.get("models") or []):
        return _err("模型不在该供应商列表内", "invalid_payload", 400)
    if not model:
        return _err("该供应商未配置默认模型", "not_configured", 400)

    result = llm_vision.call_vision(
        provider, provider["api_key"], model=model,
        prompt=prompt.strip(), image_b64=image_b64, mime=mime,
        max_tokens=_VISION_MAX_TOKENS)

    if result.get("error_code") == "ssrf_blocked":
        return _err("上游地址校验失败，已拒绝出网", "ssrf_blocked", 400)
    if result.get("error_code") == "upstream_timeout":
        return _err("上游超时或不可达，请稍后重试", "upstream_timeout", 504,
                    retryable=True)
    if not result.get("ok"):
        return _err("上游返回错误（HTTP %s）" % result.get("status"),
                    "upstream_error", 502, retryable=True)

    # 计量（仅成功调用；失败不写——错误已按响应反馈用户）
    try:
        store.record_vision_usage(
            provider_name=provider.get("name") or "", model=model,
            subject_role=ident.get("role") or "user",
            subject_id=ident.get("user_id") or "",
            user_id=ident.get("user_id") or "",
            usage=result.get("usage"))
    except Exception:
        # 计量失败不影响用户结果（诊断走服务日志，无敏感字段）
        pass

    usage = result.get("usage") or {}
    return {
        "text": result.get("text") or "",
        "provider": provider.get("name") or "",
        "model": model,
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        } if usage else None,
        "duration_ms": result.get("duration_ms", 0),
    }, 200
