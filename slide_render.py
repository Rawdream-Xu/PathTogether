# -*- coding: utf-8 -*-
"""多通道伪彩渲染共享模块（Batch 2 纯函数 + Batch 3 token/请求级解析）。

由 app.py / share_server.py 共用，禁止在三处各写一份颜色/签名算法
（规格 §7.1）。覆盖：

- Batch 2：OME ``Channel@Color`` RGBA 解析（有符号 32 位；``-1`` 是显式白
  #FFFFFFFF，与「属性不存在」严格区分）、缺色确定性色卡、
  ``global-percentile-v1`` 全局强度窗（线程级「同 generation 只算一次」）、
  canonical ``render_context`` 规范化 + SHA-256 fingerprint（``asset_revision``
  与 fingerprint 相互独立）、多通道线性加色合成与 DeepZoom
  ``RenderedSlideView`` 适配（只包装当前借出的 slide，不跨 borrow/线程缓存）；
- Batch 3（§6.2/§6.3/§7.3/§7.4）：HMAC ``render_token`` 签名/验证（独立用途
  key，从应用 secret 派生；多 worker / share_server 无共享内存可验证）、
  请求级 context 解析（token / 用户选择 / 默认方案三入口，revision 绑定，
  解码前拒绝）、info additive 字段组装、统计缓存按 slide+generation 淘汰。

Flask 路由与鉴权仍归各 app（本模块不 import Flask）。日志与指标只记安全
信息（fingerprint 前缀/通道数/耗时/错误码），不记录 token 全文或图像内容。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import math
import os
import re
import threading
import time
from typing import NamedTuple

import numpy as np
import PIL
from PIL import Image

from slide_io import (  # noqa: F401  # 再导出：调用方 import slide_render 即可
    SlideRenderError,
    TiffFileSlide,
    build_channel_index,
)

#: 对外 API（app.py / share_server.py 等调用方 `import slide_render` 即用）
__all__ = [
    # 常量
    "ALGORITHM_VERSION", "CONTEXT_VERSION_MULTICHANNEL",
    "CONTEXT_VERSION_NATIVE_RGB", "PLANE_POLICY", "MAX_ACTIVE_CHANNELS",
    "DEFAULT_ACTIVE_CHANNELS", "SAMPLE_TARGET", "DEFAULT_PALETTE",
    "NATIVE_RGB_FINGERPRINT", "RENDER_TOKEN_KEY_INFO",
    "RENDER_ERROR_HTTP", "FLAG_ENV",
    # slide_io 再导出（稳定错误契约 / duck-type / zarr 索引）
    "SlideRenderError", "TiffFileSlide", "build_channel_index",
    # 颜色与 manifest
    "default_pseudocolor", "parse_ome_channel_color", "hex_to_rgb01",
    "fluorophore_color_by_name", "FLUOROPHORE_KEYWORDS",
    "stable_channel_color",
    "build_channel_manifest", "slide_image_mode",
    # 全局统计
    "compute_global_window", "purge_stats_for",
    # context / fingerprint / 合成 / DeepZoom 适配
    "canonicalize_render_context", "build_default_render_context",
    "composite_region", "RenderedSlideView",
    # Batch 3：token 与请求级解析
    "derive_render_key", "sign_render_token", "verify_render_token",
    "issue_render_token", "resolve_render_context",
    "canonicalize_selection", "RenderRequestError",
    "build_render_info",
    # F1/F2：选层与显示 JPEG 编码
    "choose_read_level", "TILE_ENCODER_VERSION",
    "TILE_NATIVE_JPEG_QUALITY", "TILE_NATIVE_JPEG_SUBSAMPLING",
    "TILE_MULTICHANNEL_JPEG_QUALITY", "TILE_MULTICHANNEL_JPEG_SUBSAMPLING",
    "display_jpeg_params", "encode_display_jpeg", "image_mode_from_context",
    "subsampling_label",
    # 指标与测试隔离
    "metrics_inc", "metrics_add", "metrics_snapshot", "reset_caches",
]

# --------------------------------------------------------------------------- #
# 常量与版本（任何公式/统计口径变化必须升版本，不能悄改同一 fingerprint 的
# 像素结果——规格 §5.3）
# --------------------------------------------------------------------------- #
#: 全局强度窗算法版本
ALGORITHM_VERSION = "global-percentile-v1"
#: 多通道合成 context 版本
CONTEXT_VERSION_MULTICHANNEL = "multichannel-additive-v1"
#: 原生 RGB context 版本
CONTEXT_VERSION_NATIVE_RGB = "native-rgb-v1"
#: T/Z 首平面策略
PLANE_POLICY = "first-plane-v1"
#: 一次最多启用的通道数
MAX_ACTIVE_CHANNELS = 8
#: 默认启用的前 N 个有效通道
DEFAULT_ACTIVE_CHANNELS = 4
#: 全局统计每通道最多采样的有限像素数
SAMPLE_TARGET = 262144

#: 缺色确定性色卡（§5.2，按逻辑通道索引循环）
DEFAULT_PALETTE = (
    "#00FFFF",  # 0 青
    "#FF00FF",  # 1 洋红
    "#FFD166",  # 2 黄
    "#00E676",  # 3 绿
    "#FF5C5C",  # 4 红
    "#4D7CFE",  # 5 蓝
    "#FF8C42",  # 6 橙
    "#B388FF",  # 7 紫
)

#: OME 32 位有符号 Color 的合法整数区间
_INT32_RANGE = (-(2 ** 31), 2 ** 32 - 1)

# --------------------------------------------------------------------------- #
# Batch 3：feature flag、HMAC render_token、请求级稳定错误（§6.2/§6.3/§15.2）
# --------------------------------------------------------------------------- #
#: feature flag 环境变量（默认关闭；读 env 于调用时，测试可 monkeypatch）
FLAG_ENV = "PATHTOGETHER_MULTICHANNEL_ENABLED"

#: render_token 派生 key 的独立用途域（HMAC-SHA256(secret, 本域) —— 与其它
#: 用途（session/visitor 等）隔离；轮换 secret 即全体旧 token 失效）
RENDER_TOKEN_KEY_INFO = b"pathtogether-render-token-v1"

#: token payload 版本（结构变化时递增）
_TOKEN_VERSION = 1

#: 稳定错误码 → HTTP 状态（§7.4；路由层据此映射，不得自行发明映射）
RENDER_ERROR_HTTP = {
    "invalid_render_context": 400,
    "render_channel_out_of_range": 400,
    "render_channel_limit": 400,
    "unsupported_plane_selection": 400,
    "unsupported_multifile_ome": 400,
}


class RenderRequestError(Exception):
    """请求级 render context 解析失败（携带稳定机器码 + HTTP 状态）。

    与 :class:`SlideRenderError`（纯函数层校验）分离：本异常由请求级流程
    （token 验证 / revision 绑定 / flag 判定）抛出，code 与
    :data:`RENDER_ERROR_HTTP` 同词表，另含 403 ``multichannel_disabled`` 与
    409 ``slide_revision_conflict``。
    """

    def __init__(self, code, message=None, status=None):
        super().__init__(message or code)
        self.code = code
        self.status = int(status) if status is not None \
            else RENDER_ERROR_HTTP.get(code, 400)


def multichannel_enabled(env=None):
    """读 feature flag（默认关闭）。测试可传 env dict 或运行时改 os.environ。"""
    env = os.environ if env is None else env
    return str(env.get(FLAG_ENV) or "").strip().lower() in ("1", "true",
                                                            "yes", "on")


def derive_render_key(secret):
    """从应用 secret 派生 render_token 专用 key（HMAC-SHA256，独立用途域）。

    多 worker / share_server 各自从同一 secret（env 或同一 secret 文件）派生，
    无共享内存即可互相验证；secret 轮换后旧 token 全体失效。
    """
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(bytes(secret), RENDER_TOKEN_KEY_INFO,
                    hashlib.sha256).digest()


def _token_body(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True, allow_nan=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def sign_render_token(payload, secret):
    """对 payload（dict）签名，返回 ``"<b64url(body)>.<hmac-hex>"``。"""
    body = _token_body(payload)
    sig = hmac.new(derive_render_key(secret), body.encode("ascii"),
                   hashlib.sha256).hexdigest()
    return body + "." + sig


def verify_render_token(token, secret):
    """验证 token；合法返回 payload dict，任何失败返回 None（不抛异常）。

    常数时间比较；验证失败（含篡改 / secret 轮换 / 格式坏）一律 None——
    调用方统一按 ``invalid_render_context`` 4xx 处理，前端刷新 info 重建一次。
    """
    if not isinstance(token, str) or "." not in token:
        return None
    body, _, sig = token.rpartition(".")
    if not body or not sig:
        return None
    expect = hmac.new(derive_render_key(secret), body.encode("ascii"),
                      hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        pad = "=" * (-len(body) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(body + pad).decode("utf-8"))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != _TOKEN_VERSION:
        return None
    if not isinstance(payload.get("ctx"), dict) \
            or not isinstance(payload.get("rev"), str) \
            or not isinstance(payload.get("fp"), str):
        return None
    return payload


def issue_render_token(canonical_context, fingerprint, asset_revision,
                       secret, slide=""):
    """由 canonical context + fingerprint + revision 签发 render_token。

    payload 含 canonical context（不含 asset_revision/fingerprint 本身——
    revision 单独携带，fingerprint 供日志/缓存前缀）与 slide 安全名（绑定
    防跨切片重放）。token **不授予权限**：每个资源端点仍走原鉴权（§2/§6.2）。
    """
    payload = {
        "v": _TOKEN_VERSION,
        "slide": str(slide or ""),
        "rev": str(asset_revision or ""),
        "fp": str(fingerprint or ""),
        "ctx": {
            "version": canonical_context.get("version"),
            "plane": canonical_context.get("plane") or {"t": 0, "z": 0},
            "active_channels": canonical_context.get("active_channels") or [],
        },
    }
    return sign_render_token(payload, secret)


def token_context(payload):
    """从已验证的 token payload 还原完整 canonical context（补 asset_revision）。"""
    ctx = dict(payload["ctx"])
    ctx["asset_revision"] = payload["rev"]
    return ctx

# --------------------------------------------------------------------------- #
# 指标（module-level counters；Batch 3 接到现有 metrics。只计数，不记内容）
# --------------------------------------------------------------------------- #
_METRICS = {
    "manifest_built": 0,
    "manifest_seconds": 0.0,
    "stats_computed": 0,
    "stats_cache_hit": 0,
    "stats_cache_miss": 0,
    "stats_seconds": 0.0,
    "channels_decoded": 0,
    "composite_built": 0,
    "composite_seconds": 0.0,
    "nonfinite_pixels": 0,
    "context_verified": 0,
    "context_verify_fail": 0,
}
_METRICS_LOCK = threading.Lock()


def metrics_inc(name, n=1):
    """计数器自增（线程安全）。未知名忽略（防拼写错误撑爆 dict）。"""
    if name not in _METRICS:
        return
    with _METRICS_LOCK:
        _METRICS[name] += n


def metrics_add(name, seconds):
    """累计耗时（线程安全）。未知名忽略。"""
    if name not in _METRICS:
        return
    with _METRICS_LOCK:
        _METRICS[name] += float(seconds)


def metrics_snapshot():
    """返回指标副本（调用方安全读取）。"""
    with _METRICS_LOCK:
        return dict(_METRICS)


def reset_caches():
    """清空全局统计缓存与指标（测试隔离用；生产勿调）。"""
    global _STATS_CACHE, _STATS_KEY_LOCKS
    with _STATS_REGISTRY_LOCK:
        _STATS_CACHE = {}
        _STATS_KEY_LOCKS = {}
    with _METRICS_LOCK:
        for k in _METRICS:
            _METRICS[k] = 0


def purge_stats_for(scope_prefix):
    """按 slide 安全名前缀淘汰统计缓存（切片删除/关闭路径接 generation 失效）。

    统计键的 scope 由调用方提供 ``"<safe>#<generation>"`` 形态（见
    :func:`_stats_key`），本函数删除该 slide 的**全部代**条目。只动统计缓存，
    不清指标。
    """
    prefix = str(scope_prefix or "") + "#"
    with _STATS_REGISTRY_LOCK:
        stale = [k for k in _STATS_CACHE
                 if isinstance(k[0], tuple) and len(k[0]) == 2
                 and k[0][0] == "gen" and str(k[0][1]).startswith(prefix)]
        for k in stale:
            _STATS_CACHE.pop(k, None)


# --------------------------------------------------------------------------- #
# OME RGBA 解析（§5.1）与确定性色卡（§5.2）
# --------------------------------------------------------------------------- #

#: 染料/颜色名关键词 → 显示 RGB（2026-11-15 用户需求：文件名/通道名提及
#: 颜色时按该色渲染）。大小写不敏感子串匹配；**中英双语**（中文用户文件
#: 命名习惯：_红色/_绿/_DAPI 等）。slide_render（manifest 默认色）与
#: slide_io（legacy read_region 灰度路径 tint）共用同一张表，防止两条
#: 渲染路径颜色分叉。
FLUOROPHORE_KEYWORDS = (
    (("dapi", "hoechst", "蓝", "blue"), (0, 0, 255)),
    (("fitc", "gfp", "488", "绿", "green"), (0, 255, 0)),
    (("cy3", "tritc", "568", "594", "rfp", "mcherry", "红", "red"),
     (255, 0, 0)),
    (("cy5", "647", "far-red", "洋红", "品红", "magenta"),
     (255, 0, 255)),
    (("cy7", "750", "近红外", "黄", "yellow"), (255, 255, 0)),
    (("青", "cyan"), (0, 255, 255)),
    (("橙", "orange"), (255, 140, 0)),
    (("紫", "purple", "violet"), (179, 136, 255)),
)


def fluorophore_color_by_name(name):
    """按染料/颜色关键词返回显示 RGB 三元组；无匹配返回 None。

    slide_render（manifest 默认色）与 slide_io（legacy 灰度路径 tint）
    共用；匹配顺序即表序（先染料后普通颜色词）。
    """
    low = str(name or "").strip().lower()
    if not low:
        return None
    for keys, color in FLUOROPHORE_KEYWORDS:
        if any(k in low for k in keys):
            return color
    return None


def default_pseudocolor(index):
    """按逻辑通道索引循环取确定性色卡；同索引永远同色（禁随机/进程哈希）。"""
    return DEFAULT_PALETTE[int(index) % len(DEFAULT_PALETTE)]


def stable_channel_color(name):
    """按通道名稳定取显示色（hex）：染料/颜色关键词优先，否则 crc32 稳定
    散列选色卡（同名恒同色——跨进程/跨重启稳定；异名大概率异色）。

    2026-11-15 用户需求：多文件 OME 组合的孤通道切片（每切片 1 通道）
    全落色卡[0] 同色不可辨——按名散列后互不撞色。manifest 默认色与
    slide_io legacy 灰度路径 tint 共用本函数（防分叉）。
    """
    keyword = fluorophore_color_by_name(name)
    if keyword is not None:
        return _rgba_to_hex(*keyword)
    import zlib
    idx = zlib.crc32(str(name or "").encode("utf-8")) % len(DEFAULT_PALETTE)
    return DEFAULT_PALETTE[idx]


def parse_ome_channel_color(value):
    """解析 OME ``Channel@Color`` 属性为 ``(r, g, b, a)``（各 0..255）。

    - 有符号 32 位整数形态（bioformats 惯用）：
      ``u32 = signed & 0xffffffff``，``r/g/b/a`` 按 §5.1 位移公式；
      **显式 ``-1`` → 0xFFFFFFFF → #FFFFFF alpha=255（白色，来源 OME）**，
      与「属性不存在」（调用方传 None）严格区分——本函数对 None 返回 None，
      由调用方区分「缺失」与「显式值」。
    - ``#RRGGBB`` / ``#RRGGBBAA`` 十六进制形态（tifffile 2024.5.22 自身
      写出的形态）：6 位时 alpha 记 255，8 位时含 alpha。
    - 非整数/越界（超出 32 位表示范围）→ None，调用方视为缺失 + warning。
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.startswith("#"):
        h = s[1:]
        try:
            if len(h) == 6:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
            if len(h) == 8:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16),
                        int(h[6:8], 16))
        except ValueError:
            return None
        return None
    try:
        v = int(s, 10)
    except ValueError:
        return None
    if v < _INT32_RANGE[0] or v > _INT32_RANGE[1]:
        return None
    u32 = v & 0xFFFFFFFF
    return (
        (u32 >> 24) & 0xFF,
        (u32 >> 16) & 0xFF,
        (u32 >> 8) & 0xFF,
        u32 & 0xFF,
    )


def _rgba_to_hex(r, g, b):
    """(r,g,b) → 大写 6 位 hex（manifest 颜色契约 #RRGGBB）。"""
    return "#%02X%02X%02X" % (int(r) & 0xFF, int(g) & 0xFF, int(b) & 0xFF)


def hex_to_rgb01(color):
    """``#RRGGBB`` → ``(r, g, b)`` 0..1 浮点（合成公式输入）。非法返回 None。"""
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}",
                                                      color):
        return None
    h = color[1:]
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
            int(h[4:6], 16) / 255.0)


# --------------------------------------------------------------------------- #
# channel manifest（§6.1）
# --------------------------------------------------------------------------- #
def build_channel_manifest(slide, *, asset_generation=None,
                           with_intensity=False):
    """构建通道说明 manifest（纯元数据派生；可含全局统计强度窗）。

    返回 dict（§6.1 additive 字段，extra 键为内部实现细节）：
      ``image_mode``（``multichannel``/``native_rgb``）、``series_index``、
      ``axes``、``shape``、``plane``（含 ``policy: first-plane-v1``）、
      ``channels[]``（index/id/name/color/alpha/color_source/default_active/
      dtype/intensity）、``warnings[]``（结构化 ``{code, message}``）。

    - 颜色来源只允许 ``ome``（XML 属性存在且合法）或 ``default``（缺失/
      非法 → 色卡 + warning）；显式 ``-1`` 是 ome 白色。
    - Name 缺失 → 「通道 N」（1 基），不猜荧光团。
    - ``default_active``：alpha>0 且强度窗可用（若已计算）的前 4 个有效
      通道；alpha=0 / empty_or_constant 不算有效。
    - ``SizeT>1`` 或 ``SizeZ>1`` → 结构化 warning ``first-plane-v1``。
    """
    t0 = time.perf_counter()
    if not isinstance(slide, TiffFileSlide):
        metrics_add("manifest_seconds", time.perf_counter() - t0)
        metrics_inc("manifest_built")
        return {
            "image_mode": "native_rgb",
            "series_index": 0,
            "axes": "",
            "shape": (),
            "plane": {
                "t": 0, "z": 0, "size_t": 1, "size_z": 1,
                "policy": PLANE_POLICY,
            },
            "channels": [],
            "warnings": [],
        }
    plane_sizes = slide.plane_sizes or {}
    size_c = int(plane_sizes.get("size_c", 0) or 0)
    size_t = int(plane_sizes.get("size_t", 1) or 1)
    size_z = int(plane_sizes.get("size_z", 1) or 1)
    warnings = []
    if size_t > 1 or size_z > 1:
        warnings.append({
            "code": "first-plane-v1",
            "message": "当前仅显示 T=0、Z=0；时间/层面切换尚未支持",
        })
    manifest = {
        "image_mode": "native_rgb" if slide.is_native_rgb else "multichannel",
        "series_index": int(slide.series_index),
        "axes": str(slide.axes),
        "shape": [int(v) for v in (slide.shape or ())],
        "plane": {"t": 0, "z": 0, "size_t": size_t, "size_z": size_z,
                  "policy": PLANE_POLICY},
        "channels": [],
        "warnings": warnings,
    }
    if manifest["image_mode"] == "native_rgb":
        metrics_inc("manifest_built")
        metrics_add("manifest_seconds", time.perf_counter() - t0)
        return manifest

    ome = slide.ome_channels
    if ome is None:
        ome = []
    elif len(ome) != size_c:
        warnings.append({
            "code": "ome_channel_mismatch",
            "message": "OME-XML 通道描述数(%d)与像素 SizeC(%d)不一致，"
                       "未匹配通道按缺失处理" % (len(ome), size_c),
        })
    entries = []
    for i in range(size_c):
        meta = ome[i] if i < len(ome) else None
        name = (meta or {}).get("name")
        color_raw = (meta or {}).get("color")
        alpha = 1.0
        if color_raw is None:
            # 2026-11-15：染料/颜色名关键词（DAPI→蓝 等）优先；单通道
            # （孤通道切片）按名稳定散列取色——异名异色；多通道按索引色卡
            keywords_rgb = fluorophore_color_by_name(name)
            if keywords_rgb is not None:
                color_hex = _rgba_to_hex(*keywords_rgb)
                source = "default"
            elif size_c == 1 and name:
                color_hex = stable_channel_color(name)
                source = "default"
            else:
                color_hex = default_pseudocolor(i)
                source = "default"
        else:
            rgba = parse_ome_channel_color(color_raw)
            if rgba is None:
                warnings.append({
                    "code": "ome_color_invalid",
                    "message": "通道 %d 的 OME Color=%r 无法解析，已改用"
                               "默认伪彩色卡" % (i, color_raw),
                })
                color_hex = default_pseudocolor(i)
                source = "default"
            else:
                r, g, b, a = rgba
                color_hex = _rgba_to_hex(r, g, b)
                source = "ome"
                alpha = a / 255.0
        if alpha <= 0.0:
            warnings.append({
                "code": "ome_channel_alpha_zero",
                "message": "通道 %d 的 OME 颜色 alpha=0，默认关闭；"
                           "用户重选颜色后按不透明处理" % i,
            })
        entries.append({
            "index": i,
            "id": (meta or {}).get("id")
                  or "Channel:%d:%d" % (int(slide.series_index), i),
            "name": (str(name).strip() if name else "") or ("通道 %d" % (i + 1)),
            "color": color_hex,
            "alpha": round(alpha, 4),
            "color_source": source,
            "default_active": False,  # 由 _finalize_default_active 回填
            "dtype": str(slide.dtype),
            "intensity": None,
        })
    manifest["channels"] = entries
    if with_intensity:
        for e in entries:
            st = compute_global_window(slide, e["index"],
                                       asset_generation=asset_generation)
            e["intensity"] = {
                "black": st["black"],
                "white": st["white"],
                "gamma": st["gamma"],
                "source": st["source"],
                "status": st["status"],
            }
    _finalize_default_active(entries, warnings)
    metrics_inc("manifest_built")
    metrics_add("manifest_seconds", time.perf_counter() - t0)
    return manifest


def _finalize_default_active(entries, warnings):
    """默认启用前 N 个「有效」通道（alpha>0 且强度窗可用）。

    注意：默认方案里没有可用通道时（全部 alpha=0 / empty_or_constant）
    不伪造默认——置 no_default_channel warning，由上层提示。
    """
    for e in entries:
        inten = e.get("intensity")
        ok = e["alpha"] > 0 and (
            inten is None or inten.get("status") != "empty_or_constant")
        e["default_active"] = False
        if ok:
            e["_valid"] = True
    valid = [e for e in entries if e.get("_valid")]
    for e in entries:
        e.pop("_valid", None)
    for e in valid[:DEFAULT_ACTIVE_CHANNELS]:
        e["default_active"] = True
    for e in entries:
        inten = e.get("intensity")
        if inten is not None and inten.get("status") == "empty_or_constant":
            warnings.append({
                "code": "channel_not_displayable",
                "message": "通道 %d 强度范围不可用（空/常量数据），默认关闭"
                           % e["index"],
            })
    if entries and not any(e["default_active"] for e in entries):
        warnings.append({
            "code": "no_default_channel",
            "message": "没有可默认显示的通道（全部不可用）；请手动选择",
        })


# --------------------------------------------------------------------------- #
# 全局强度窗 global-percentile-v1（§5.3）
# --------------------------------------------------------------------------- #
# 统计缓存：key → 结果 dict；同 key 只允许一个线程计算，其余等待/复用。
# 上限保护：超过 _STATS_CACHE_MAX 条时整体清空（Batch 3 接 generation 淘汰）。
_STATS_CACHE = {}
_STATS_KEY_LOCKS = {}
_STATS_CACHE_MAX = 4096
_STATS_REGISTRY_LOCK = threading.Lock()


def _stats_key(slide, channel_index, asset_generation, t, z):
    """统计缓存键（§5.3：generation/series/t/z/算法版本 + 逻辑通道）。

    ``asset_generation`` 由调用方提供（Batch 3 路由传当前文件 generation，
    跨 slide 实例共享缓存）；未提供时退化为 **实例域** 键（id(slide)），
    避免不同文件同 (series, t, z, channel) 串统计。
    """
    scope = ("gen", str(asset_generation)) if asset_generation is not None \
        else ("inst", id(slide))
    return (
        scope,
        int(slide.series_index),
        int(t),
        int(z),
        ALGORITHM_VERSION,
        int(channel_index),
    )


def compute_global_window(slide, channel_index, *, asset_generation=None,
                          t=0, z=0):
    """计算（或复用）单通道全局强度窗 ``global-percentile-v1``。

    - 从**可用最低分辨率金字塔层**做确定性网格采样，有限像素 ≤ 262,144；
      不加载 level-0 全图（单层文件该层即最低层，用网格步长避免整面载入）。
    - ``black=P0.1``、``white=P99.9``、``gamma=1.0``；
    - ``white<=black``：整数回退 dtype 有效范围；float 回退有限 min/max；
      仍无范围 → ``status="empty_or_constant"``；
    - NaN/Inf 不参与统计；结果带 ``nonfinite`` 计数（合成时按 0 处理）。
    - 缓存键 ``(scope, series, t, z, algorithm, channel)``：scope 为调用方
      传入的 ``asset_generation``（Batch 3），未传时退化为 slide 实例域；
      同 key 同一时刻只允许一个线程计算，其余等待后复用（无新依赖）。

    返回 dict：``{channel_index, black, white, gamma, source, status,
    samples, nonfinite, level}``（副本，调用方可安全改写）。
    """
    channel_index = int(channel_index)
    if channel_index < 0 or channel_index >= int(slide.channel_count):
        raise SlideRenderError(
            "render_channel_out_of_range",
            "通道索引 %d 越界（逻辑通道数 %d）"
            % (channel_index, int(slide.channel_count)))
    t = int(t)
    z = int(z)
    if t != 0 or z != 0:
        raise SlideRenderError(
            "unsupported_plane_selection",
            "首版仅支持 T=0,Z=0（policy %s）" % PLANE_POLICY)
    key = _stats_key(slide, channel_index, asset_generation, t, z)
    with _STATS_REGISTRY_LOCK:
        lock = _STATS_KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _STATS_KEY_LOCKS[key] = lock
    with lock:
        cached = _STATS_CACHE.get(key)
        if cached is not None:
            metrics_inc("stats_cache_hit")
            return dict(cached)
        metrics_inc("stats_cache_miss")
        t0 = time.perf_counter()
        result = _compute_global_window(slide, channel_index, t, z)
        if len(_STATS_CACHE) >= _STATS_CACHE_MAX:
            _STATS_CACHE.clear()  # 简单上限保护（代价是重算，不出错）
        _STATS_CACHE[key] = result
        metrics_inc("stats_computed")
        metrics_add("stats_seconds", time.perf_counter() - t0)
        return dict(result)


def _grid_step(height, width):
    """确定性网格步长：采样点数 ≤ SAMPLE_TARGET 的最小步长（从 1 起步）。"""
    step = 1
    while (len(range(0, int(height), step))
           * len(range(0, int(width), step))) > SAMPLE_TARGET:
        step += 1
    return step


def _compute_global_window(slide, channel_index, t, z):
    """实际统计（调用方已持锁；结果 dict 会被缓存，勿改写）。"""
    level = int(slide.level_count) - 1  # 可用的最低分辨率金字塔层
    if level < 0:
        level = 0
    arr = slide.level_arrays[level]
    axes = slide.level_axes[level]
    lshape = slide.level_shapes[level]
    iy = axes.index("Y")
    ix = axes.index("X")
    height = int(lshape[iy])
    width = int(lshape[ix])
    step = _grid_step(height, width)
    idx = build_channel_index(axes, channel_index, t, z,
                              slice(0, height, step), slice(0, width, step))
    sample = np.asarray(arr[idx])
    dtype = sample.dtype
    finite_mask = np.isfinite(sample) if dtype.kind == "f" else \
        np.ones(sample.shape, dtype=bool)
    nonfinite = int(np.count_nonzero(~finite_mask))
    if nonfinite:
        metrics_inc("nonfinite_pixels", nonfinite)
    vals = sample[finite_mask].astype(np.float64)
    result = {
        "channel_index": channel_index,
        "black": None,
        "white": None,
        "gamma": 1.0,
        "source": ALGORITHM_VERSION,
        "status": "empty_or_constant",
        "samples": int(vals.size),
        "nonfinite": nonfinite,
        "level": level,
    }
    if vals.size:
        black = float(np.percentile(vals, 0.1))
        white = float(np.percentile(vals, 99.9))
        if white > black:
            result["black"], result["white"], result["status"] = \
                black, white, "ok"
        elif dtype.kind in ("u", "i"):
            info = np.iinfo(dtype)
            result["black"], result["white"], result["status"] = \
                float(info.min), float(info.max), "ok"
        elif dtype.kind == "b":
            result["black"], result["white"], result["status"] = \
                0.0, 1.0, "ok"
        else:
            # float：回退全局有限 min/max；仍无范围 → empty_or_constant
            fmin = float(vals.min())
            fmax = float(vals.max())
            if fmax > fmin:
                result["black"], result["white"], result["status"] = \
                    fmin, fmax, "ok"
    return result


# --------------------------------------------------------------------------- #
# canonical render context 与 fingerprint（§6.2 纯函数部分）
# --------------------------------------------------------------------------- #
_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}$")


def _round4(v):
    """固定 4 位小数；规范化 -0.0 → 0.0（canonical 序列化确定性）。"""
    r = round(float(v), 4)
    if r == 0.0:
        return 0.0
    return r


def _num(v):
    """严格数值（bool 不算）：有限 float 或 None。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if not math.isfinite(f):
        return None
    return f


def _require(cond, code, message):
    if not cond:
        raise SlideRenderError(code, message)


def canonicalize_render_context(ctx, *, channel_count=None):
    """规范化 render context，返回 ``(canonical_dict, fingerprint)``。

    - 校验（§6.2 步骤 2 的纯函数部分）：版本、asset_revision 非空、plane
      为非负整数（首版仅 T=0,Z=0，任何非 0 平面 →
      ``unsupported_plane_selection``）、active_channels 唯一且
      1..MAX_ACTIVE_CHANNELS（native-rgb-v1 必须为空）、颜色 6 位十六进制、
      alpha 0..1、black/white 有限且 white>black、gamma 0.1..5；通道越界 →
      ``render_channel_out_of_range``；超 8 → ``render_channel_limit``；
      其余结构问题 → ``invalid_render_context``。
    - canonical：active_channels 按 index 升序、颜色大写、浮点固定 4 位
      小数、-0.0 规范化为 0.0 → 字段顺序/写法不同但语义相同的输入得到
      完全相同的 canonical dict。
    - fingerprint：对 ``{version, plane, active_channels}``（**不含**
      asset_revision/fingerprint 本身——两者相互独立）做
      ``json.dumps(sort_keys=True, separators=(",", ":"))`` 后取
      SHA-256 lowercase hex。
    """
    _require(isinstance(ctx, dict), "invalid_render_context",
             "render context 必须是对象")
    version = ctx.get("version")
    _require(version in (CONTEXT_VERSION_MULTICHANNEL,
                         CONTEXT_VERSION_NATIVE_RGB),
             "invalid_render_context", "未知 context 版本: %r" % (version,))
    revision = ctx.get("asset_revision")
    _require(isinstance(revision, str) and bool(revision),
             "invalid_render_context", "asset_revision 必须是非空字符串")

    plane = ctx.get("plane")
    if plane is None:
        plane = {"t": 0, "z": 0}
    _require(isinstance(plane, dict), "invalid_render_context",
             "plane 必须是对象")
    t = plane.get("t", 0)
    z = plane.get("z", 0)
    for name, v in (("t", t), ("z", z)):
        _require(not isinstance(v, bool) and isinstance(v, int) and v >= 0,
                 "invalid_render_context",
                 "plane.%s 必须是非负整数" % name)
    _require(t == 0 and z == 0, "unsupported_plane_selection",
             "首版仅支持 T=0,Z=0（policy %s）" % PLANE_POLICY)

    channels_in = ctx.get("active_channels")
    if version == CONTEXT_VERSION_NATIVE_RGB:
        _require(not channels_in, "invalid_render_context",
                 "native-rgb-v1 不接受 active_channels")
        channels = []
    else:
        _require(isinstance(channels_in, list) and bool(channels_in),
                 "invalid_render_context",
                 "active_channels 必须是非空数组")
        _require(len(channels_in) <= MAX_ACTIVE_CHANNELS,
                 "render_channel_limit",
                 "一次最多启用 %d 个通道（收到 %d）"
                 % (MAX_ACTIVE_CHANNELS, len(channels_in)))
        seen = set()
        cleaned = []
        for item in channels_in:
            _require(isinstance(item, dict), "invalid_render_context",
                     "active_channels 条目必须是对象")
            index = item.get("index")
            _require(not isinstance(index, bool) and isinstance(index, int)
                     and index >= 0, "invalid_render_context",
                     "通道 index 必须是非负整数")
            if channel_count is not None:
                _require(index < int(channel_count),
                         "render_channel_out_of_range",
                         "通道索引 %d 越界（逻辑通道数 %s）"
                         % (index, channel_count))
            _require(index not in seen, "invalid_render_context",
                     "通道 index 重复: %d" % index)
            seen.add(index)
            color = item.get("color")
            _require(isinstance(color, str)
                     and _HEX_COLOR_RE.fullmatch(color),
                     "invalid_render_context",
                     "通道 %d 颜色必须是 6 位十六进制 #RRGGBB" % index)
            alpha = _num(item.get("alpha"))
            _require(alpha is not None and 0.0 <= alpha <= 1.0,
                     "invalid_render_context",
                     "通道 %d alpha 必须在 0..1" % index)
            black = _num(item.get("black"))
            white = _num(item.get("white"))
            _require(black is not None and white is not None
                     and white > black, "invalid_render_context",
                     "通道 %d black/white 必须有限且 white>black" % index)
            gamma = _num(item.get("gamma"))
            _require(gamma is not None and 0.1 <= gamma <= 5.0,
                     "invalid_render_context",
                     "通道 %d gamma 必须在 0.1..5" % index)
            cleaned.append({
                "index": index,
                "color": color.upper(),
                "alpha": _round4(alpha),
                "black": _round4(black),
                "white": _round4(white),
                "gamma": _round4(gamma),
            })
        cleaned.sort(key=lambda c: c["index"])
        channels = cleaned
    canonical = {
        "version": version,
        "asset_revision": revision,
        "plane": {"t": t, "z": z},
        "active_channels": channels,
    }
    fingerprint = _fingerprint({
        "version": version,
        "plane": {"t": t, "z": z},
        "active_channels": channels,
    })
    return canonical, fingerprint


def _fingerprint(payload):
    """canonical JSON（排序键、紧凑分隔符）→ SHA-256 lowercase hex。"""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_default_render_context(slide, manifest, asset_revision):
    """由 manifest 构建默认 render context（未 canonicalize 的输入形态）。

    - native_rgb → ``native-rgb-v1`` + 空 active_channels；
    - multichannel → manifest 中 ``default_active`` 的通道，强度窗缺失时
      现算（走缓存）；``empty_or_constant`` 通道不进默认方案；
    - 没有可用通道 → 返回 None（调用方提示，不伪造默认）。
    """
    if manifest.get("image_mode") == "native_rgb":
        return {
            "version": CONTEXT_VERSION_NATIVE_RGB,
            "asset_revision": asset_revision,
            "plane": {"t": 0, "z": 0},
            "active_channels": [],
        }
    active = []
    for entry in manifest.get("channels", []):
        if not entry.get("default_active"):
            continue
        inten = entry.get("intensity")
        if inten is None:
            st = compute_global_window(slide, entry["index"])
        elif inten.get("status") == "empty_or_constant":
            continue
        else:
            st = inten
        if (st.get("status") == "empty_or_constant"
                or st.get("black") is None or st.get("white") is None
                or not st["white"] > st["black"]):
            continue
        active.append({
            "index": int(entry["index"]),
            "color": entry["color"],
            "alpha": float(entry["alpha"]),
            "black": float(st["black"]),
            "white": float(st["white"]),
            "gamma": float(st.get("gamma", 1.0)),
        })
    if not active:
        return None
    return {
        "version": CONTEXT_VERSION_MULTICHANNEL,
        "asset_revision": asset_revision,
        "plane": {"t": 0, "z": 0},
        "active_channels": active,
    }


#: native-rgb-v1 的固定 fingerprint（cache key 的 legacy 项；同指纹对所有
#: RGB 切片一致，key 中另有 safe_name + generation 隔离）
NATIVE_RGB_FINGERPRINT = _fingerprint({
    "version": CONTEXT_VERSION_NATIVE_RGB,
    "plane": {"t": 0, "z": 0},
    "active_channels": [],
})


def slide_image_mode(slide):
    """切片显示模式：``native_rgb`` / ``multichannel``（§6.1 image_mode）。

    普通图片（raster_slide.RasterSlide，BMP/JPEG）**显式** native_rgb：转
    显示 RGB 后就是单幅彩色图，绝不允许进入荧光通道流程（is_raster_image
    守卫先于 isinstance——灰度/调色板/CMYK 已在读入器内归一 RGB，没有
    channel_count/axes 之类的通道属性可依赖）。
    非 TiffFileSlide（OpenSlide 厂商格式 SVS/NDPI…）一律 native_rgb；
    TiffFileSlide 按 photometric/S/C 判定（is_native_rgb / channel_count）。
    """
    if getattr(slide, "is_raster_image", False):
        return "native_rgb"
    if not isinstance(slide, TiffFileSlide):
        return "native_rgb"
    if slide.is_native_rgb:
        return "native_rgb"
    return "multichannel"


def canonicalize_selection(slide, body, *, asset_revision,
                           asset_generation=None):
    """render-context POST 端点的服务端规范化（§6.2 步骤 2-4）。

    请求只提交用户选择 ``active_channels[]``（每项 index 必填，颜色/alpha/
    black/white/gamma 可省略——缺省由 manifest 强度窗与色卡补齐）、可选
    ``plane``。RGB 切片不接受 active_channels（native-rgb-v1）。

    返回 ``(canonical_dict, fingerprint)``；校验失败抛
    :class:`SlideRenderError`（稳定码，路由层映射 4xx）。通道数超限在**任何
    解码/统计之前**拒绝（§7.4）。
    """
    body = body if isinstance(body, dict) else {}
    plane = body.get("plane")
    channels_in = body.get("active_channels")
    if slide_image_mode(slide) == "native_rgb":
        ctx_in = {
            "version": CONTEXT_VERSION_NATIVE_RGB,
            "asset_revision": asset_revision,
            "plane": plane,
            "active_channels": channels_in or [],
        }
        return canonicalize_render_context(ctx_in, channel_count=0)

    if isinstance(channels_in, list) \
            and len(channels_in) > MAX_ACTIVE_CHANNELS:
        # 解码前拒绝：先于逐通道强度窗计算（§7.4）
        raise SlideRenderError(
            "render_channel_limit",
            "一次最多启用 %d 个通道（收到 %d）"
            % (MAX_ACTIVE_CHANNELS, len(channels_in)))
    manifest = build_channel_manifest(slide, asset_generation=asset_generation,
                                      with_intensity=True)
    by_index = {e["index"]: e for e in manifest.get("channels", [])}
    active = []
    if channels_in is None:
        channels_in = []
    for item in channels_in:
        if not isinstance(item, dict):
            raise SlideRenderError("invalid_render_context",
                                   "active_channels 条目必须是对象")
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise SlideRenderError("invalid_render_context",
                                   "通道 index 必须是非负整数")
        entry = by_index.get(index)
        if entry is None:
            raise SlideRenderError(
                "render_channel_out_of_range",
                "通道索引 %d 不存在（逻辑通道数 %d）"
                % (index, len(by_index)))
        inten = entry.get("intensity")
        if inten is None:
            inten = compute_global_window(slide, index,
                                          asset_generation=asset_generation)
        if inten.get("status") == "empty_or_constant" \
                or inten.get("black") is None or inten.get("white") is None:
            raise SlideRenderError(
                "invalid_render_context",
                "通道 %d 强度范围不可用（空/常量数据），不能启用" % index)
        active.append({
            "index": int(index),
            "color": item.get("color") if item.get("color") is not None
            else entry["color"],
            "alpha": item.get("alpha") if item.get("alpha") is not None
            else entry["alpha"],
            "black": item.get("black") if item.get("black") is not None
            else inten["black"],
            "white": item.get("white") if item.get("white") is not None
            else inten["white"],
            "gamma": item.get("gamma") if item.get("gamma") is not None
            else inten.get("gamma", 1.0),
        })
    ctx_in = {
        "version": CONTEXT_VERSION_MULTICHANNEL,
        "asset_revision": asset_revision,
        "plane": plane,
        "active_channels": active,
    }
    return canonicalize_render_context(
        ctx_in, channel_count=len(manifest.get("channels", [])))


def resolve_render_context(slide, *, safe="", expected_revision="",
                           token=None, token_secret=None, body=None,
                           asset_generation=None, flag_enabled=True):
    """请求级 render context 统一解析（§6.3 资源端点 + §6.2 POST 端点共用）。

    返回 ``(context, fingerprint)``；``context is None`` 表示走 native/legacy
    路径（旧像素语义）。失败抛 :class:`RenderRequestError`（稳定码 + 状态）：

    - flag 关 → 忽略 token/body，返回 (None, None)（资源端点 legacy；POST
      端点由路由先行按 ``multichannel_disabled`` 403）；
    - token：验签（独立用途派生 key）→ slide 绑定 → revision 绑定（不符 →
      409 ``slide_revision_conflict``）；无效 → 400 ``invalid_render_context``；
    - body（用户选择）→ :func:`canonicalize_selection`；
    - 缺省：multichannel → 服务端默认 context；native RGB → (None, None)。

    ``expected_revision`` 是当前切片 asset revision（mtime:size 形态的既有
    ``_legacy_slide_revision``）；token 中 rev 与之不符即 409。
    """
    if not flag_enabled:
        return None, None
    if token:
        payload = verify_render_token(token, token_secret)
        if payload is None:
            metrics_inc("context_verify_fail")
            raise RenderRequestError(
                "invalid_render_context",
                "render token 无效（可能已被篡改或密钥轮换，请刷新 info 重建）",
                400)
        if safe and payload.get("slide") and payload["slide"] != safe:
            metrics_inc("context_verify_fail")
            raise RenderRequestError("invalid_render_context",
                                     "render token 与切片不匹配", 400)
        if str(payload.get("rev") or "") != str(expected_revision or ""):
            metrics_inc("context_verify_fail")
            raise RenderRequestError(
                "slide_revision_conflict",
                "render token 绑定的切片资产已更新，请刷新 info 重建一次",
                409)
        metrics_inc("context_verified")
        return token_context(payload), payload["fp"]
    if body is not None:
        canonical, fp = canonicalize_selection(
            slide, body, asset_revision=expected_revision,
            asset_generation=asset_generation)
        metrics_inc("context_verified")
        return canonical, fp
    # 缺省：默认 context（multichannel）；native RGB 走 legacy
    if slide_image_mode(slide) != "multichannel":
        return None, None
    manifest = build_channel_manifest(slide, asset_generation=asset_generation,
                                      with_intensity=True)
    dctx = build_default_render_context(slide, manifest, expected_revision)
    if dctx is None:
        return None, None
    canonical, fp = canonicalize_render_context(
        dctx, channel_count=len(manifest.get("channels", [])))
    metrics_inc("context_verified")
    return canonical, fp


def build_render_info(slide, *, asset_revision, asset_generation=None,
                      secret=None, slide_name="", flag_enabled=True):
    """组装 info 响应的 additive 字段（§6.1；app.py 与 share_server 共用）。

    返回 dict（由调用方 merge 进既有 info）：
      - flag 关：``image_mode`` + ``server_capability``（探测用，不泄露更多）；
      - flag 开 + RGB：``image_mode/channels:[]/default_render_context
        (native-rgb-v1)/default_render_token/warnings/plane``；
      - flag 开 + 多通道：另含 ``axes``、含强度窗的 channels、
        默认 context + token（无可用默认通道时 context/token 为 None +
        结构化 warning）。
    ``asset_revision`` 与 ``render_context_fingerprint`` 是两个独立值。
    """
    mode = slide_image_mode(slide)
    out = {
        "image_mode": mode,
        "asset_revision": str(asset_revision or ""),
    }
    if not flag_enabled:
        out["server_capability"] = {
            "multichannel": False,
            "render_token": False,
            "render_context_endpoint": False,
        }
        return out
    out["server_capability"] = {
        "multichannel": mode == "multichannel",
        "render_token": True,
        "render_context_endpoint": True,
    }
    # OpenSlide 厂商格式（SVS/NDPI…）没有 plane_sizes：不得走 OME manifest。
    if mode == "native_rgb":
        out["channels"] = []
        out["warnings"] = []
        out["plane"] = {
            "t": 0, "z": 0, "size_t": 1, "size_z": 1, "policy": PLANE_POLICY,
        }
        dctx = {
            "version": CONTEXT_VERSION_NATIVE_RGB,
            "asset_revision": str(asset_revision or ""),
            "plane": {"t": 0, "z": 0},
            "active_channels": [],
        }
        canonical, fp = canonicalize_render_context(dctx, channel_count=0)
        out["default_render_context"] = dict(canonical, fingerprint=fp)
        out["default_render_token"] = issue_render_token(
            canonical, fp, asset_revision, secret, slide=slide_name)
        return out
    manifest = build_channel_manifest(slide, asset_generation=asset_generation,
                                      with_intensity=True)
    warnings = list(manifest.get("warnings", []))
    out["channels"] = manifest.get("channels", [])
    out["warnings"] = warnings
    out["plane"] = manifest.get("plane")
    if mode == "multichannel":
        out["axes"] = manifest.get("axes")
    dctx = build_default_render_context(slide, manifest, asset_revision)
    if dctx is None:
        out["default_render_context"] = None
        out["default_render_token"] = None
        if mode == "multichannel" and not any(
                w.get("code") == "no_default_channel" for w in warnings):
            warnings.append({
                "code": "no_default_channel",
                "message": "没有可默认显示的通道（全部不可用）；请手动选择",
            })
        return out
    canonical, fp = canonicalize_render_context(
        dctx, channel_count=len(manifest.get("channels", [])))
    out["default_render_context"] = dict(canonical, fingerprint=fp)
    out["default_render_token"] = issue_render_token(
        canonical, fp, asset_revision, secret, slide=slide_name)
    return out


# --------------------------------------------------------------------------- #
# 多通道区域合成（§5.3 multichannel-additive-v1）
# --------------------------------------------------------------------------- #
def composite_region(slide, context, location, level, size):
    """按 canonical context 合成区域，返回 PIL RGBA（尺寸恰为 size）。

    ``n_i = clamp((raw_i - black_i) / (white_i - black_i), 0, 1) ** (1/gamma)``，
    ``rgb = clamp(sum(n_i * alpha_i * color_i_rgb), 0, 1)``（线性加色、
    顺序无关），统一量化 sRGB uint8。NaN/Inf 按 0 处理并计指标。
    越界/padding 语义与 ``read_region`` 一致：有效区不透明（a=255），
    越界区全透明。

    只接受 ``multichannel-additive-v1``；native RGB 由
    :class:`RenderedSlideView` 直通旧 ``read_region``。通道越界在解码前
    拒绝（``render_channel_out_of_range``）。
    普通图片（is_raster_image）没有 channel_count/read_region_channels，
    正常请求流不可能到达这里（slide_image_mode 恒 native_rgb）；防御性
    守卫把「伪造/串位的 multichannel context」从 AttributeError 收敛为
    稳定 ``invalid_render_context``（§4.5 错误契约）。
    """
    t0 = time.perf_counter()
    if getattr(slide, "is_raster_image", False):
        raise SlideRenderError(
            "invalid_render_context",
            "普通图片（BMP/JPEG）为单幅 RGB，不支持多通道合成")
    _require(isinstance(context, dict)
             and context.get("version") == CONTEXT_VERSION_MULTICHANNEL,
             "invalid_render_context",
             "composite_region 需要 multichannel-additive-v1 context")
    channels = context.get("active_channels") or []
    _require(bool(channels), "invalid_render_context",
             "active_channels 不能为空")
    plane = context.get("plane") or {}
    t = int(plane.get("t", 0))
    z = int(plane.get("z", 0))
    indices = [int(c["index"]) for c in channels]
    count = int(slide.channel_count)
    for i in indices:  # 防御：解码前再校验（canonicalize 已保证）
        if i < 0 or i >= count:
            raise SlideRenderError(
                "render_channel_out_of_range",
                "通道索引 %d 越界（逻辑通道数 %d）" % (i, count))
    w = int(size[0])
    h = int(size[1])
    planes, geom = slide.read_region_channels(location, level, (w, h),
                                              indices, t=t, z=z)
    metrics_inc("channels_decoded", len(indices))
    out = np.zeros((h, w, 3), dtype=np.float64)
    alpha_mask = np.zeros((h, w), dtype=np.uint8)
    vw = geom["valid_w"]
    vh = geom["valid_h"]
    if vw > 0 and vh > 0:
        acc = np.zeros((vh, vw, 3), dtype=np.float64)
        for cfg, raw in zip(channels, planes):
            black = float(cfg["black"])
            white = float(cfg["white"])
            gamma = float(cfg["gamma"])
            alpha = float(cfg["alpha"])
            rgb01 = hex_to_rgb01(cfg["color"])
            _require(rgb01 is not None, "invalid_render_context",
                     "通道 %d 颜色非法: %r" % (cfg["index"], cfg["color"]))
            finite = np.isfinite(raw)
            nf = int(np.count_nonzero(~finite))
            if nf:
                metrics_inc("nonfinite_pixels", nf)
            data = np.where(finite, raw, 0.0)
            n = (data - black) / (white - black)
            np.clip(n, 0.0, 1.0, out=n)
            n = np.power(n, 1.0 / gamma)
            acc += n[..., None] * (alpha * np.asarray(rgb01))
        np.clip(acc, 0.0, 1.0, out=acc)
        u8 = np.rint(acc * 255.0).astype(np.uint8)
        px = geom["paste_x"]
        py = geom["paste_y"]
        out[py:py + vh, px:px + vw, :] = u8
        alpha_mask[py:py + vh, px:px + vw] = 255
    rgba = np.empty((h, w, 4), dtype=np.uint8)
    rgba[..., :3] = out.astype(np.uint8)
    rgba[..., 3] = alpha_mask
    img = Image.fromarray(rgba)  # (h,w,4) → RGBA（不传已废弃的 mode 参数）
    metrics_inc("composite_built")
    metrics_add("composite_seconds", time.perf_counter() - t0)
    return img


# --------------------------------------------------------------------------- #
# DeepZoom RenderedSlideView（§7.1/§7.3）
# --------------------------------------------------------------------------- #
class RenderedSlideView:
    """包装**当前借出的** slide 应用 render context（OpenSlide duck-type）。

    - 供 ``openslide.deepzoom.DeepZoomGenerator`` 直接使用：
      ``properties``/``dimensions``/``level_dimensions``/``level_downsamples``/
      ``level_count``/``get_best_level_for_downsample``/``read_region``/
      ``get_thumbnail``。
    - ``native-rgb-v1``（或未提供 context）→ 直通底层 ``read_region``；
      ``multichannel-additive-v1`` → :func:`composite_region`。
    - 不持有底层生命周期：:meth:`close` 是 no-op，slide 由借用方关闭；
      **不要**把绑定某一池句柄的 wrapper 跨 borrow 或跨线程缓存。
    """

    def __init__(self, slide, context=None, manifest=None, fingerprint=None):
        self._slide = slide
        self.context = context
        self.manifest = manifest
        self.fingerprint = fingerprint

    # ---- OpenSlide duck-type 委托 ----
    @property
    def properties(self):
        return self._slide.properties

    @property
    def dimensions(self):
        return self._slide.dimensions

    @property
    def level_count(self):
        return self._slide.level_count

    @property
    def level_dimensions(self):
        return self._slide.level_dimensions

    @property
    def level_downsamples(self):
        return self._slide.level_downsamples

    def get_best_level_for_downsample(self, downsample):
        return self._slide.get_best_level_for_downsample(downsample)

    def read_region(self, location, level, size):
        """按 context 读取区域（native 直通 / multichannel 合成）。"""
        ctx = self.context
        if (ctx is None
                or ctx.get("version") == CONTEXT_VERSION_NATIVE_RGB
                or not ctx.get("active_channels")):
            return self._slide.read_region(location, level, size)
        return composite_region(self._slide, ctx, location, level, size)

    def get_thumbnail(self, size):
        """从最低层读整图（应用 context）后缩放，返回 PIL RGBA。"""
        w, h = size
        idx = int(self._slide.level_count) - 1
        lw, lh = self._slide.level_dimensions[idx]
        img = self.read_region((0, 0), idx, (lw, lh))
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        tw, th = img.size
        scale = min(float(w) / tw if tw else 1.0,
                    float(h) / th if th else 1.0)
        new_w = max(1, int(round(tw * scale)))
        new_h = max(1, int(round(th * scale)))
        return img.resize((new_w, new_h), Image.LANCZOS)

    def close(self):
        """no-op：不关闭借出的 slide（生命周期归借用方）。"""
        return None


# --------------------------------------------------------------------------- #
# F1 选层 + F2 显示 JPEG 编码（2026-09-05 批次；app.py / share_server.py 共用）
# --------------------------------------------------------------------------- #
def choose_read_level(level_downsamples, src_w, src_h, out_w, out_h):
    """按输出尺寸诚实选金字塔层，返回 ``(level, downsample, upsampled)``。

    - 先有合法 out 尺寸（调用方完成 clamp / 默认最长边），再选层；
    - ``max_ds = min(src_w/out_w, src_h/out_h)``；小于 1 说明 out 比 src 还大
      （要放大）→ level 0 + ``upsampled=True``；
    - 否则在 ``downsample <= max_ds`` 的层里选**最粗**层（够用即可），绝不选
      需要再放大的层。不把 OpenSlide ``get_best_level_for_downsample`` 当
      权威（它可能偏向更大 downsample）。
    """
    downsamples = [max(1.0, float(d)) for d in (level_downsamples or (1.0,))]
    ow = max(1, int(out_w))
    oh = max(1, int(out_h))
    max_ds = min(float(src_w) / ow, float(src_h) / oh)
    if max_ds < 1.0:
        return 0, downsamples[0], True
    level = 0
    for i, ds in enumerate(downsamples):
        if ds <= max_ds:
            level = i
    return level, downsamples[level], False


#: 显示编码器版本（公式/参数变化必须升版本；瓦片缓存键含此值）
TILE_ENCODER_VERSION = "display-jpeg-v2"
#: native（明场/RGB）瓦片：显式 4:2:0（Pillow subsampling=2）
TILE_NATIVE_JPEG_QUALITY = 82
TILE_NATIVE_JPEG_SUBSAMPLING = 2
#: 多通道合成：显式 4:4:4（subsampling=0），高饱和细色线不被色度抽样糊掉
TILE_MULTICHANNEL_JPEG_QUALITY = 95
TILE_MULTICHANNEL_JPEG_SUBSAMPLING = 0


def image_mode_from_context(ctx):
    """瓦片/region 编码用的 image_mode：只有真正的多通道合成才走 4:4:4。

    flag 打开时 RGB 切片仍带 ``native-rgb-v1`` context（ctx 非 None），不得
    因此被当成荧光编码。
    """
    if isinstance(ctx, dict) and ctx.get("version") == CONTEXT_VERSION_MULTICHANNEL:
        return "multichannel"
    return "native_rgb"


def display_jpeg_params(image_mode, quality=None):
    """按 image_mode 返回显式 ``(quality, subsampling)``（禁止靠 Pillow 默认）。

    ``quality`` 非空时覆盖该模式的默认 quality（region 派生图仍走调用方
    jpeg_quality），subsampling 只由 image_mode 决定。
    """
    if image_mode == "multichannel":
        q = TILE_MULTICHANNEL_JPEG_QUALITY if quality is None else int(quality)
        return q, TILE_MULTICHANNEL_JPEG_SUBSAMPLING
    q = TILE_NATIVE_JPEG_QUALITY if quality is None else int(quality)
    return q, TILE_NATIVE_JPEG_SUBSAMPLING


def encode_display_jpeg(img, *, image_mode, quality=None):
    """display-jpeg-v2 编码，返回 ``(jpeg_bytes, params)``。

    native：quality=82、subsampling=2（4:2:0）；multichannel：quality=95、
    subsampling=0（4:4:4）。``params`` 回传实际参数（含 encoder_version），
    供缓存键与响应回显。

    字节契约红线（§3.1）：本函数服务 region/AI 导数路径，禁止为 viewer
    优化改签名、默认参数或输出字节（optimize/progressive 显式 False）。
    """
    q, subsampling = display_jpeg_params(image_mode, quality)
    buf = io.BytesIO()
    _jpeg_save(img, buf, quality=q, subsampling=subsampling,
               optimize=False, progressive=False)
    return buf.getvalue(), {
        "quality": q,
        "subsampling": subsampling,
        "encoder_version": TILE_ENCODER_VERSION,
    }


#: Pillow subsampling 整数 → 色度抽样契约标签（encoder info 回显用；
#: 与上面 TILE_*_SUBSAMPLING 常量同处一文件，避免两处漂移）
SUBSAMPLING_LABELS = {0: "4:4:4", 1: "4:2:2", 2: "4:2:0"}


def subsampling_label(subsampling):
    """Pillow subsampling 整数 → 色度抽样标签（"4:4:4"/"4:2:2"/"4:2:0"）。

    只做展示层映射；未知值如实回 "unknown"，不猜测。
    """
    return SUBSAMPLING_LABELS.get(int(subsampling), "unknown")


# =========================================================================== #
# viewer 显示编码（image-transport-upgrade §3.1/§3.2）
#
# 与 region/AI 导数编码（encode_display_jpeg）**严格隔离**：
#   - encode_display_jpeg 的签名、默认参数、输出字节、metadata、版本
#     （TILE_ENCODER_VERSION）保持原样——region/派生图的 content_sha256
#     契约不容漂移（§3.1 红线，golden 测试护栏）；
#   - viewer 瓦片/缩略图走独立的 resolve_viewer_encoding → encode_viewer_jpeg
#     → viewer_encoding_fingerprint 三件套；编码与缓存读同一 spec，禁止各处
#     重新猜模式或质量。
# =========================================================================== #

#: viewer 编码实现版本（profile 参数/实现变化必须升版本；与 TILE_ENCODER_VERSION
#: 独立命名，避免影响 region 的既有元数据契约）
VIEWER_ENCODER_VERSION = "viewer-encoding-v1"
#: 客户端 URL/协议管线版本（进 display_version 摘要；URL 形态变化时升）
VIEWER_PIPELINE_VERSION = "viewer-pipeline-v1"

#: 瓦片 profiles（§3.2 配置表）
VIEWER_PROFILE_NATIVE_STANDARD = "native-standard-v1"
VIEWER_PROFILE_NATIVE_DETAIL = "native-detail-v1"
VIEWER_PROFILE_FLUORESCENCE = "fluorescence-preserve-v1"
#: 缩略图 profiles（主站/分享）
VIEWER_PROFILE_NATIVE_THUMB = "native-thumb-v1"
VIEWER_PROFILE_FLUORESCENCE_THUMB = "fluorescence-thumb-v1"

#: 服务端白名单（URL 提供之外的 profile 一律 400）
VIEWER_TILE_PROFILES = (
    VIEWER_PROFILE_NATIVE_STANDARD,
    VIEWER_PROFILE_NATIVE_DETAIL,
    VIEWER_PROFILE_FLUORESCENCE,
)
VIEWER_THUMBNAIL_PROFILES = (
    VIEWER_PROFILE_NATIVE_THUMB,
    VIEWER_PROFILE_FLUORESCENCE_THUMB,
)

#: 精细档标定质量（§7.2 规则：84/86/88/90 从低到高取**首个**同时满足
#: "边缘 RGB MAE 中位数降低≥10%、SSIM 中位数不下降、总 tile bytes ≤标准
#: 1.35 倍"的档位。真实样本基准（55 ROI，bench-samples 5 张 HE + 报告
#: artifacts/.../B6/cal-q*）实测：q84 bytes=1.231、edge MAE 改善 16.4%、
#: SSIM +0.001 → 全达标；q86 bytes=1.371 超预算 → 固定 84。
VIEWER_DETAIL_CALIBRATED_QUALITY = 84
#: 荧光 preserve 质量/采样（§3.2；与 TILE_MULTICHANNEL_JPEG_* 同值但独立常量，
#: 独立演进：viewer 侧变化不改 region 字节契约）
VIEWER_FLUORESCENCE_QUALITY = 95
VIEWER_FLUORESCENCE_SUBSAMPLING = 0
#: 缩略图质量（RGB thumb 与既有 save(quality=90) 同 q；荧光 thumb 用 preserve q95）
VIEWER_THUMB_QUALITY = 90


def validate_display_quality(value, name="JPEG_QUALITY"):
    """JPEG 质量配置校验：1..100 整数，非法明确失败（不静默重置为 82）。

    启动时（app.py / share_server.py import）调用；非法配置抛 ValueError
    直接失败，保留合法的覆盖值。
    """
    try:
        q = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s 必须是 1..100 的整数，当前值 %r" % (name, value))
    if not 1 <= q <= 100:
        raise ValueError("%s 必须是 1..100 的整数，当前值 %r" % (name, value))
    return q


class ViewerEncodingSpec(NamedTuple):
    """不可变 viewer 编码 spec：编码、缓存键、display_version 三者同源。"""

    profile_id: str        # 白名单 profile 或 "legacy"（旧 URL 无 profile 语义）
    purpose: str           # "tile" | "thumbnail"
    image_mode: str        # "native_rgb" | "multichannel"（解析后的 context 决定）
    quality: int
    subsampling: int
    optimize: bool
    progressive: bool
    format: str            # 恒 "JPEG"
    encoder_version: str   # VIEWER_ENCODER_VERSION（legacy spec 沿用 TILE_ 版本）
    runtime_identity: tuple  # (pillow, jpeg_codec, viewer_encoder) 定型元组


def _viewer_runtime_identity():
    """实际 Pillow / JPEG 库版本 + viewer 编码实现版本（镜像升级会改显示身份）。

    与 AI 导数编码无关：region 路径不读取本值，升级 Pillow 不会改变 region
    字节，但会改变 viewer display_version（如实反映显示输出已变化）。
    """
    from PIL import features as _features

    return (PIL.__version__, _features.version("jpg") or "unknown",
            VIEWER_ENCODER_VERSION)


_VIEWER_RUNTIME_CACHE = None


def viewer_runtime_identity():
    """模块级缓存的 runtime identity（值不可变，缓存安全）。"""
    global _VIEWER_RUNTIME_CACHE
    if _VIEWER_RUNTIME_CACHE is None:
        _VIEWER_RUNTIME_CACHE = _viewer_runtime_identity()
    return _VIEWER_RUNTIME_CACHE


def _legacy_viewer_spec(image_mode, purpose, quality):
    """旧 URL（无 profile）的精确 spec：编码现状原样（optimize/progressive 关）。

    tile：quality 由调用方传运行时 JPEG_QUALITY（修复键 q82/字节 q<env> 缺陷
    ——键与字节从此同源）；thumbnail RGB：q90（与既有 save(JPEG, quality=90)
    字节一致）；荧光 thumbnail 走 preserve spec（§4.7 修复，不适用本函数）。
    """
    if image_mode == "multichannel":
        subsampling = TILE_MULTICHANNEL_JPEG_SUBSAMPLING
    else:
        subsampling = TILE_NATIVE_JPEG_SUBSAMPLING
    return ViewerEncodingSpec(
        profile_id="legacy", purpose=purpose, image_mode=image_mode,
        quality=int(quality), subsampling=subsampling,
        optimize=False, progressive=False, format="JPEG",
        encoder_version=TILE_ENCODER_VERSION,
        runtime_identity=viewer_runtime_identity())


def resolve_viewer_encoding(render_mode, purpose, profile_id=None, *,
                            quality_override=None):
    """按 render_mode + purpose + profile 解析不可变 EncodingSpec（§3.1）。

    - ``profile_id=None``：旧 URL 的 legacy spec（保持既有参数语义）；
    - 多通道资源只接受 preserve 档；RGB 档传给多通道资源抛
      ``RenderRequestError("invalid_display_profile", status=400)``，不静默
      按 4:2:0 输出；
    - 模式判断以解析后的 context 为准（native-rgb-v1 不是多通道——调用方传
      image_mode_from_context(ctx) 的结果，本函数不做"ctx 非空/颜色多"猜测）；
    - 精细档质量 = max(标定质量, 质量覆盖)（§3.2：用户已配 q95 时精细不降质）。
    ``quality_override``：标准档质量（服务端 JPEG_QUALITY 运行时值）。
    """
    if purpose not in ("tile", "thumbnail"):
        raise ValueError("viewer purpose 仅支持 tile/thumbnail，收到 %r"
                         % (purpose,))
    if render_mode not in ("native_rgb", "multichannel"):
        raise ValueError("viewer render_mode 仅支持 native_rgb/multichannel，"
                         "收到 %r" % (render_mode,))
    multichannel = render_mode == "multichannel"
    if profile_id is None:
        if purpose == "thumbnail" and multichannel:
            # 荧光 thumbnail 修复（§4.7）：不再依赖 Pillow 默认色度采样
            return ViewerEncodingSpec(
                profile_id=VIEWER_PROFILE_FLUORESCENCE_THUMB,
                purpose=purpose, image_mode=render_mode,
                quality=VIEWER_FLUORESCENCE_QUALITY,
                subsampling=VIEWER_FLUORESCENCE_SUBSAMPLING,
                optimize=True, progressive=False, format="JPEG",
                encoder_version=VIEWER_ENCODER_VERSION,
                runtime_identity=viewer_runtime_identity())
        return _legacy_viewer_spec(
            render_mode, purpose,
            VIEWER_THUMB_QUALITY if purpose == "thumbnail"
            else (quality_override if quality_override is not None
                  else TILE_NATIVE_JPEG_QUALITY))
    if purpose == "tile":
        if multichannel:
            if profile_id != VIEWER_PROFILE_FLUORESCENCE:
                raise RenderRequestError(
                    "invalid_display_profile",
                    "多通道资源仅接受 %s 档" % VIEWER_PROFILE_FLUORESCENCE,
                    status=400)
            return ViewerEncodingSpec(
                profile_id=profile_id, purpose=purpose,
                image_mode=render_mode,
                quality=VIEWER_FLUORESCENCE_QUALITY,
                subsampling=VIEWER_FLUORESCENCE_SUBSAMPLING,
                optimize=True, progressive=False, format="JPEG",
                encoder_version=VIEWER_ENCODER_VERSION,
                runtime_identity=viewer_runtime_identity())
        if profile_id == VIEWER_PROFILE_NATIVE_STANDARD:
            q = int(quality_override) if quality_override is not None \
                else TILE_NATIVE_JPEG_QUALITY
            return ViewerEncodingSpec(
                profile_id=profile_id, purpose=purpose,
                image_mode=render_mode, quality=validate_display_quality(
                    q, "viewer standard quality"),
                subsampling=TILE_NATIVE_JPEG_SUBSAMPLING,
                optimize=True, progressive=False, format="JPEG",
                encoder_version=VIEWER_ENCODER_VERSION,
                runtime_identity=viewer_runtime_identity())
        if profile_id == VIEWER_PROFILE_NATIVE_DETAIL:
            base = VIEWER_DETAIL_CALIBRATED_QUALITY
            if quality_override is not None:
                base = max(base, int(quality_override))
            return ViewerEncodingSpec(
                profile_id=profile_id, purpose=purpose,
                image_mode=render_mode, quality=base,
                subsampling=TILE_MULTICHANNEL_JPEG_SUBSAMPLING,  # 4:4:4
                optimize=True, progressive=False, format="JPEG",
                encoder_version=VIEWER_ENCODER_VERSION,
                runtime_identity=viewer_runtime_identity())
        raise RenderRequestError(
            "invalid_display_profile", "未知显示 profile %r" % (profile_id,),
            status=400)
    # purpose == "thumbnail"
    if multichannel:
        if profile_id != VIEWER_PROFILE_FLUORESCENCE_THUMB:
            raise RenderRequestError(
                "invalid_display_profile",
                "多通道缩略图仅接受 %s 档" % VIEWER_PROFILE_FLUORESCENCE_THUMB,
                status=400)
        return ViewerEncodingSpec(
            profile_id=profile_id, purpose=purpose, image_mode=render_mode,
            quality=VIEWER_FLUORESCENCE_QUALITY,
            subsampling=VIEWER_FLUORESCENCE_SUBSAMPLING,
            optimize=True, progressive=False, format="JPEG",
            encoder_version=VIEWER_ENCODER_VERSION,
            runtime_identity=viewer_runtime_identity())
    if profile_id != VIEWER_PROFILE_NATIVE_THUMB:
        raise RenderRequestError(
            "invalid_display_profile",
            "RGB 缩略图仅接受 %s 档" % VIEWER_PROFILE_NATIVE_THUMB,
            status=400)
    return ViewerEncodingSpec(
        profile_id=profile_id, purpose=purpose, image_mode=render_mode,
        quality=VIEWER_THUMB_QUALITY,
        subsampling=TILE_NATIVE_JPEG_SUBSAMPLING,
        optimize=True, progressive=False, format="JPEG",
        encoder_version=VIEWER_ENCODER_VERSION,
        runtime_identity=viewer_runtime_identity())


def _jpeg_save(img, buf, *, quality, subsampling, optimize, progressive):
    """显式参数的底层 JPEG save 原语（display 与 viewer 编码共享）。

    encode_display_jpeg 以 optimize=False, progressive=False 调用本原语，
    Pillow 调用形状与历史实现逐参一致（字节不变）。
    """
    img.save(buf, format="JPEG", quality=quality, subsampling=subsampling,
             optimize=optimize, progressive=progressive)


def encode_viewer_jpeg(img, spec):
    """按 EncodingSpec 编码，返回 ``(jpeg_bytes, actual_encoding)``。

    ``actual_encoding`` 回传实际参数（含 optimize 与 runtime），供缓存键、
    ETag 与响应头回显；编码只读 spec，不做任何模式/质量二次猜测。
    """
    buf = io.BytesIO()
    _jpeg_save(img, buf, quality=spec.quality, subsampling=spec.subsampling,
               optimize=spec.optimize, progressive=spec.progressive)
    return buf.getvalue(), {
        "profile_id": spec.profile_id,
        "purpose": spec.purpose,
        "image_mode": spec.image_mode,
        "quality": spec.quality,
        "subsampling": spec.subsampling,
        "optimize": bool(spec.optimize),
        "progressive": bool(spec.progressive),
        "format": spec.format,
        "encoder_version": spec.encoder_version,
        "runtime_identity": list(spec.runtime_identity),
    }


def viewer_encoding_fingerprint(spec):
    """最终规范化 spec 的确定性摘要（SHA256，canonical JSON）。

    profile 名称不变但参数/运行时变化 → 指纹变化：显示身份不能被 profile
    名复用欺骗。digest 不含路径、token、坐标。
    """
    payload = {
        "profile_id": spec.profile_id,
        "purpose": spec.purpose,
        "image_mode": spec.image_mode,
        "quality": int(spec.quality),
        "subsampling": int(spec.subsampling),
        "optimize": bool(spec.optimize),
        "progressive": bool(spec.progressive),
        "format": spec.format,
        "encoder_version": spec.encoder_version,
        "runtime_identity": list(spec.runtime_identity),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def viewer_encoding_spec_for_test(image_mode, *, quality, subsampling,
                                  optimize):
    """测试辅助：直接构造 viewer spec（绕过 profile 白名单做纯函数断言）。"""
    return ViewerEncodingSpec(
        profile_id="test", purpose="tile", image_mode=image_mode,
        quality=int(quality), subsampling=int(subsampling),
        optimize=bool(optimize), progressive=False, format="JPEG",
        encoder_version=VIEWER_ENCODER_VERSION,
        runtime_identity=viewer_runtime_identity())


def display_encoding_info(render_mode, purpose, *, quality_override=None):
    """info 响应 ``display.profiles`` 的每档描述（§5.2；不泄露 cache key）。

    按 render_mode 过滤白名单：荧光只出 preserve 档（§3.2），RGB 只出
    standard/detail（缩略图同理），不列出会 400 的组合。
    """
    if render_mode == "multichannel":
        pids = (VIEWER_PROFILE_FLUORESCENCE,) if purpose == "tile" \
            else (VIEWER_PROFILE_FLUORESCENCE_THUMB,)
    elif purpose == "tile":
        pids = (VIEWER_PROFILE_NATIVE_STANDARD, VIEWER_PROFILE_NATIVE_DETAIL)
    else:
        pids = (VIEWER_PROFILE_NATIVE_THUMB,)
    out = []
    for pid in pids:
        spec = resolve_viewer_encoding(render_mode, purpose, pid,
                                       quality_override=quality_override)
        out.append({
            "profile_id": spec.profile_id,
            "quality": spec.quality,
            "subsampling": subsampling_label(spec.subsampling),
            "optimize": bool(spec.optimize),
            "encoder_version": spec.encoder_version,
            "encoding_fingerprint": viewer_encoding_fingerprint(spec),
        })
    return out
