# -*- coding: utf-8 -*-
"""LLM 视觉调用适配层（P1-W0b，纯函数、无 Flask/app 依赖）。

**首日决策（docs/llm-vision-w0-decisions.md D1）**：只实现
``openai_compat`` 单适配器——``POST {base_url}/chat/completions`` +
``Authorization: Bearer`` + ``image_url``（base64 data URL）。Claude/Gemini
走各自官方 OpenAI 兼容端点（base_url 模板见决策文档），不写原生协议；
``build_request`` 按 provider 的 adapter 分派，为未来原生适配器留扩展位。

安全红线：
  - 上游调用**只**由本模块发起（服务端代理），``allow_redirects=False``；
  - base_url 白名单 http/https；``assert_base_url`` 在保存时与**每次请求前**
    都校验 host（字面量 + DNS 解析复核，防 DNS rebinding TOCTOU），拒绝
    localhost/环回/私网/保留/链路本地/组播/元数据地址；env
    ``LLM_VISION_ALLOW_PRIVATE_UPSTREAM=1`` 仅放开**私网**（自建 vLLM），
    环回/localhost/元数据永不放开；
  - api_key 只进 Authorization 头，绝不进日志（本模块不 log 请求体/头）。

测试注入点：``_host_ips``（DNS）与模块级 ``requests``（transport）均可
monkeypatch——回归测试全部 mock，不真实外呼。
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
from urllib.parse import urlparse

import requests

#: 出网异常类（模块级固化：测试替换 llm_vision.requests 后 except 仍匹配真 requests 异常）
_REQUESTS_EXC = (requests.ConnectionError, requests.Timeout)

#: 连通测试用的 1x1 透明 PNG（确定性常量，70 字节 base64）
TEST_IMAGE_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                      "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

TEST_IMAGE_MIME = "image/png"

#: 上游调用超时（秒）：连通（建连）/读各一档
UPSTREAM_TIMEOUT = (10, 120)

#: base_url 允许的图片 data URL mime 白名单（vision 请求体透传前复核）
ALLOWED_IMAGE_MIMES = ("image/jpeg", "image/png")

_SSRF_BLOCKED_HOSTS = frozenset({
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.goog",
    "metadata",
    "kubernetes.default",
    "kubernetes.default.svc",
})
_SSRF_BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("255.255.255.255/32"),
)


class BaseUrlInvalid(ValueError):
    """base_url 不合法或指向禁入目标（SSRF 防线）。"""


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    """环回/私网/链路本地/保留/组播/未指定/CGNAT 等不可作为上游目标。

    env ``LLM_VISION_ALLOW_PRIVATE_UPSTREAM=1`` 时仅放开 ``is_private``
    （自建内网 vLLM 场景，部署运维显式决策）；环回/链路本地/元数据等
    其余禁入类别**永不放开**。
    """
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    allow_private = os.environ.get("LLM_VISION_ALLOW_PRIVATE_UPSTREAM") == "1"
    if allow_private and ip.is_private and not (
            ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return True
    for net in _SSRF_BLOCKED_NETWORKS:
        try:
            if ip in net:
                return True
        except TypeError:
            continue
    return False


def _host_ips(hostname: str):
    """解析主机名为 ip_address 列表（供 SSRF 检查；测试 monkeypatch 点）。"""
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BaseUrlInvalid("无法解析 base_url 主机名") from exc
    out, seen = [], set()
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        key = str(ip)
        if key not in seen:
            seen.add(key)
            out.append(ip)
    if not out:
        raise BaseUrlInvalid("无法解析 base_url 主机名")
    return out


def validate_base_url(url: str, *, resolve: bool = True) -> str:
    """校验并规范化 base_url；非法抛 :class:`BaseUrlInvalid`，合法返回原串。

    - scheme 限 http/https；无 userinfo；host 非空；无 fragment；
    - 字面 IP / DNS 解析结果不得落入禁入网段（``resolve=False`` 跳过 DNS，
      供离线场景）；末尾 ``/chat/completions`` 容忍（owner 常整段粘贴）。
    """
    if not isinstance(url, str):
        raise BaseUrlInvalid("base_url 必须是字符串")
    url = url.strip()
    if not url:
        raise BaseUrlInvalid("base_url 不能为空")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BaseUrlInvalid("base_url 仅支持 http 或 https")
    if parsed.username or parsed.password:
        raise BaseUrlInvalid("base_url 不得包含用户名或密码")
    if parsed.fragment:
        raise BaseUrlInvalid("base_url 不得包含 fragment")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise BaseUrlInvalid("base_url 缺少主机名")
    if host in _SSRF_BLOCKED_HOSTS or host.endswith(".localhost"):
        raise BaseUrlInvalid("base_url 不得指向内网、回环或云元数据地址")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and _ip_is_blocked(literal):
        raise BaseUrlInvalid("base_url 不得指向内网、回环或云元数据地址")
    if resolve:
        for ip in _host_ips(host):
            if _ip_is_blocked(ip):
                raise BaseUrlInvalid("base_url 不得指向内网、回环或云元数据地址")
    return url


#: 兼容 URL 归一：owner 粘贴整段 .../chat/completions 时剥掉端点后缀
def chat_completions_url(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    for suffix in ("/chat/completions",):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base + "/chat/completions"


def build_request(provider: dict, *, model: str, prompt: str,
                  image_b64: str, mime: str, max_tokens: int | None = None):
    """构造 OpenAI 兼容 chat.completions 请求体（adapter 分派点）。

    W0 只有 ``openai_compat``；新增原生适配器时在此按
    ``provider["adapter"]`` 分派（决策文档 D1 的扩展位）。返回 (url, body)。
    """
    if provider.get("adapter", "openai_compat") != "openai_compat":
        raise ValueError("不支持的 adapter: %s" % provider.get("adapter"))
    if mime not in ALLOWED_IMAGE_MIMES:
        raise ValueError("不支持的图片 mime: %s" % mime)
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url",
         "image_url": {"url": "data:%s;base64,%s" % (mime, image_b64)}},
    ]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
    }
    if max_tokens:
        body["max_tokens"] = int(max_tokens)
    return chat_completions_url(provider["base_url"]), body


def parse_response(status: int, payload) -> dict:
    """解析上游响应 → ``{ok, status, text, usage, error_message}``。

    content 兼容 string 与 segments 数组两种形态；usage 缺失记 None。
    """
    out = {"ok": 200 <= status < 300, "status": int(status),
           "text": "", "usage": None, "error_message": ""}
    if isinstance(payload, dict):
        choices = payload.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            c = msg.get("content")
            if isinstance(c, str):
                out["text"] = c
            elif isinstance(c, list):
                out["text"] = "".join(
                    seg.get("text", "") for seg in c
                    if isinstance(seg, dict) and seg.get("type") == "text")
        usage = payload.get("usage")
        if isinstance(usage, dict):
            out["usage"] = usage
        if not out["ok"]:
            err = payload.get("error")
            if isinstance(err, dict):
                out["error_message"] = str(err.get("message") or "")
            elif err:
                out["error_message"] = str(err)
    return out


def call_vision(provider: dict, api_key: str, *, model: str, prompt: str,
                image_b64: str, mime: str, max_tokens: int | None = None):
    """发起一次视觉调用（服务端代理；失败/超时归一为结果 dict，绝不抛网错）。

    返回 ``parse_response`` 结果 + ``duration_ms`` / ``error_code``：
      - ``upstream_timeout``：连接失败/超时（retryable）；
      - ``ssrf_blocked``：请求前 host 复核未过（未出网）。
    api_key 只进 Authorization 头；本函数不记录任何请求细节日志。
    """
    try:
        url = validate_base_url(provider.get("base_url") or "")
    except BaseUrlInvalid:
        return {"ok": False, "status": 0, "text": "", "usage": None,
                "error_message": "base_url 校验失败", "error_code": "ssrf_blocked",
                "duration_ms": 0}
    full_url, body = build_request(provider, model=model, prompt=prompt,
                                   image_b64=image_b64, mime=mime,
                                   max_tokens=max_tokens)
    headers = {"Authorization": "Bearer %s" % api_key,
               "Content-Type": "application/json"}
    import time as _time
    started = _time.monotonic()
    try:
        resp = requests.post(full_url, json=body, headers=headers,
                             timeout=UPSTREAM_TIMEOUT, allow_redirects=False)
    except _REQUESTS_EXC:
        return {"ok": False, "status": 0, "text": "", "usage": None,
                "error_message": "上游不可达或超时", "error_code": "upstream_timeout",
                "duration_ms": int((_time.monotonic() - started) * 1000)}
    try:
        payload = resp.json()
    except Exception:
        payload = None
    out = parse_response(resp.status_code, payload)
    out["duration_ms"] = int((_time.monotonic() - started) * 1000)
    out.setdefault("error_code",
                   None if out["ok"] else "upstream_error")
    return out


def normalize_usage(usage) -> dict:
    """上游 usage → ai_usage_events 列形态（OpenAI 命名 → 平台命名）。"""
    if not isinstance(usage, dict):
        return {}
    def _int(key):
        val = usage.get(key)
        return int(val) if isinstance(val, (int, float)) else None
    out = {
        "cache_hit_input_tokens": 0 if _int("prompt_tokens") is not None else None,
        "cache_miss_input_tokens": _int("prompt_tokens"),
        "output_tokens": _int("completion_tokens"),
        "total_tokens": _int("total_tokens"),
    }
    # CHECK：total = cache_hit + cache_miss + output（缺分项则整体置空，不臆造）
    if None in (out["cache_miss_input_tokens"], out["output_tokens"],
                out["total_tokens"]):
        return {}
    return out
