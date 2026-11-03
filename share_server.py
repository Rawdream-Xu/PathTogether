#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切片分享 —— 独立 Flask 服务（view-only，对外）。

监听 0.0.0.0:38000（可用 SHARE_PORT 覆盖）。
所有 /s/<token>/... 路由先校验 token 有效，再校验 slide 归属于该分享。
与主应用通过共享 JSON 文件（share_store）+ 共享上传目录（UPLOAD_DIR）交换数据。
"""

import hashlib
import hmac
import io
import os
import secrets
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    make_response,
    render_template,
    request,
    send_file,
    send_from_directory,
)
from werkzeug.utils import secure_filename

from openslide.deepzoom import DeepZoomGenerator

import share_store
import share_shared
import slide_cache
import slide_io
# slide ID 化重构 P1-B2（docs/slide-id-refactor-p1-contract-20260925.md §3/§7）：
# 本进程是 app 之外第二个切片读者——legacy 名一律经 slide_store.resolve_legacy_alias
# 解析 + share_slides(token, slide_id) 成员判定（R-04：shares.slides JSONB 不再
# 参与授权判定）+ asset_state='ready' 门禁；文件定位走
# slide_storage.resolve_descriptor_path。share_slides 成员查询为本进程自写 SQL
# （pg_store 连接习惯与 share_store_pg 同款）。
import pg_store
import slide_store
import slide_storage
# 标注可见性统一判定（工单 A / P0 数据隔离，0056）：分享端按
# visitor subject 过滤，策略与主站同一实现，不复制第二份。
import annotation_access
# 展示 J（公开分享页红线）：登录用户作者身份在 /s/* 评论出口统一掩码
# （email/login_id 经 mask_login_id，不外泄完整邮箱、不回退 display_name）。
import registration_store
import user_store
# 多通道伪彩渲染（规格 §7.1 共享模块）：颜色/签名/校验算法全部在
# slide_render——本文件只做 Flask 侧粘合，禁止复制第二份实现。
import slide_render
# P0-A §3.5：与主站 app.py 共用的 crop 像素闸（同一实现，防两份逻辑漂移）
import crop_guard
# viewer 显示编码协议（image-transport-upgrade）：display 身份/参数校验与
# 瓦片内存缓存（bytes LRU + single-flight + 指标）为共享实现，不复制策略。
import tile_cache
import viewer_display
# 标定 provenance（TMAP06 转换产物声明「不采用推算值」）：与主应用/worker
# 共用同一有界 IFD0 判定，不复制第二份字符串表。
import upload_direct_class

app = Flask(__name__)

# 上传目录与主应用共享（容器内挂载同一卷）
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or (Path.home() / "svs-viewer" / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 说明：本服务**不维护**独立扩展名词表。历史上这里有一份从 app.py 复制的
# ``SUPPORTED_EXTS``（厂商格式词表），但全文件零引用（分享端只做
# _sanitize_name + 分享成员校验 + slide_io.open_slide 按后缀分发），已删除
# 以免词表漂移；格式能力的唯一来源是 slide_format_registry / slide_io
# （LOGICAL_EXTS / is_raster_ext），app.SUPPORTED_EXTS 仅是主站上传白名单。

# Deep Zoom 参数（512 瓦片降低公网请求数，渐进式 q82 JPEG 降体积并支持模糊→清晰预览）
DZ_TILE_SIZE = 512
DZ_OVERLAP = 1
# JPEG 编码质量，可由环境变量 JPEG_QUALITY 覆盖（默认 82；1..100 整数，
# 非法配置启动即失败——image-transport-upgrade §3.2，与主站同一校验）
JPEG_QUALITY = slide_render.validate_display_quality(
    os.environ.get("JPEG_QUALITY") or 82)
# 保留旧名（与主应用一致，实际值与 DZ_* 一致）
TILE_SIZE = DZ_TILE_SIZE
OVERLAP = DZ_OVERLAP

# 切片句柄池与元数据缓存（与主应用共享 slide_cache 抽象，进程独立）
# 瓦片内存缓存（LRU + TTL）：image-transport-upgrade §6：数量上限 + 字节预算
# （TILE_CACHE_MAX_BYTES，默认 192 MiB/worker）任一先到即淘汰；键含编码身份
# （encoding_fingerprint，与编码同源）；并发 miss single-flight。TTL 只是容量
# 兜底，不再承担正确性（generation 由 slide_cache 文件签名统一驱动）。分享端
# 只读不 evict，换代完全依赖借用时的跨进程签名检查。
TILE_CACHE_MAX = int(os.environ.get("TILE_CACHE_MAX") or 3000)
TILE_CACHE_MAX_BYTES = int(os.environ.get("TILE_CACHE_MAX_BYTES")
                           or (192 * 1024 * 1024))
TILE_CACHE_TTL = float(os.environ.get("TILE_CACHE_TTL") or 3600)  # 秒
_tile_cache = tile_cache.TileMemoryCache(
    max_entries=TILE_CACHE_MAX, max_bytes=TILE_CACHE_MAX_BYTES,
    sizer=lambda v: len(v[1]))
_tile_cache_lock = threading.Lock()
_tile_single_flight = tile_cache.SingleFlight()
_viewer_metrics = tile_cache.ViewerMetrics()

# 默认 context (fp, image_mode) 的 per-(safe, generation) 缓存（与主站同语义）：
# 无 render 参数的瓦片请求在借句柄前命中缓存/精确 spec。
# （_ctx_scope 定义在下方多通道 render context 区块——此前本文件曾重复定义
# 两次、后者遮蔽前者，已收敛为一份。）
_DEFAULT_FP_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_DEFAULT_FP_CACHE_MAX = 4096
_DEFAULT_FP_LOCK = threading.Lock()


def _default_fp_cached(safe: str, generation):
    with _DEFAULT_FP_LOCK:
        hit = _DEFAULT_FP_CACHE.get((safe, generation))
    if hit is None:
        return None
    if isinstance(hit, tuple):
        return hit
    return hit, None


def _default_fp_store(safe: str, generation, fp: str,
                      image_mode=None) -> None:
    with _DEFAULT_FP_LOCK:
        if len(_DEFAULT_FP_CACHE) >= _DEFAULT_FP_CACHE_MAX:
            _DEFAULT_FP_CACHE.clear()
        _DEFAULT_FP_CACHE[(safe, generation)] = (fp, image_mode)


def _tile_cache_get(key):
    """LRU+TTL 命中：未过期才返回，过期条目按未命中处理（TTL 仅容量兜底）。"""
    item = _tile_cache.get(key)
    if item is None:
        return None
    ts, data = item
    if time.time() - ts > TILE_CACHE_TTL:
        return None
    return data


def _tile_cache_put(key, data):
    """LRU 写入（带写入时间戳作 TTL 兜底），数量/字节双预算淘汰。"""
    _tile_cache.put(key, (time.time(), data))


# --------------------------------------------------------------------------- #
# 辅助函数（从 app.py 复制，保持一致）
# --------------------------------------------------------------------------- #
def _sanitize_name(name: str) -> str:
    """净化文件名：防路径穿越同时保留中文等 Unicode 字符。"""
    if not name or "\x00" in name:
        return ""
    has_non_ascii = any(ord(c) > 127 for c in name)
    if not has_non_ascii:
        return secure_filename(name)
    cleaned_chars = []
    for ch in name:
        if ch in "/\\:" or ord(ch) < 32:
            continue
        cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars).strip().rstrip(".")
    cleaned = cleaned.replace("..", "")
    return cleaned


def _get_slide(name: str, path=None):
    """从缓存获取（或创建）切片的句柄池 entry（惰性打开，见 slide_cache）。

    P1-B2：path 为 resolver 解析后的绝对路径（读通道主路径——
    slide_storage.resolve_descriptor_path 产物）；缺省按旧口径
    ``UPLOAD_DIR / name``（无 descriptor 的调用方）。文件缺失 → 404（不变）。
    """
    if path is None:
        path = UPLOAD_DIR / name
    if not Path(path).is_file():
        abort(404, "切片不存在")
    return slide_cache.get_slide(name, path)


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _mpp_from_tiff_resolution(path: Path):
    """从 TIFF 分辨率标签读取 mpp（与主应用逻辑相同）。"""
    try:
        from PIL import Image
        from PIL.TiffTags import TAGS_V2  # noqa: F401

        with Image.open(str(path)) as img:
            tags = getattr(img, "tag_v2", None)
            if not tags:
                return None, None
            x_res = _to_float(tags.get(282))
            y_res = _to_float(tags.get(283))
            unit = tags.get(296, 2)
            if not x_res or x_res <= 0:
                return None, None
            factor = 25400.0 if unit == 2 else (10000.0 if unit == 3 else None)
            if factor is None:
                return None, None
            mpp_x = factor / x_res
            mpp_y = factor / y_res if y_res and y_res > 0 else mpp_x
            if 0.05 <= mpp_x <= 3.0:
                return mpp_x, mpp_y
    except Exception:
        pass
    return None, None


def _read_metadata(osr, path: Path) -> dict:
    """读取尺寸与 mpp 元数据（与主应用逻辑相同）。

    普通图片（BMP/JPEG，raster-image-compatibility §4.4）短路：无物理标尺，
    ``mpp_x/mpp_y/objective`` 一律 null、``mpp_source="missing"``——BMP/JPEG
    的 DPI / EXIF 分辨率不得当作组织的 µm/px，也不补默认 0.25 MPP 或 40×，
    更不走下面的 TIFF 分辨率/倍率估算探测。
    """
    width, height = osr.dimensions
    if getattr(osr, "is_raster_image", False) \
            or slide_io.is_raster_ext(str(path)):
        return {
            "width": width,
            "height": height,
            "mpp_x": None,
            "mpp_y": None,
            "objective": None,
            "mpp_source": "missing",
        }
    props = osr.properties
    objective_f = _to_float(props.get("openslide.objective-power"))

    # TMAP06 转换产物：头部像素尺寸未经标定核验 → 不得从 objective 反推
    # 默认 MPP（0.25）；分享查看器与主站同口径按未知标定（像素）显示。
    if upload_direct_class.declares_unknown_physical_scale(path):
        return {
            "width": width,
            "height": height,
            "mpp_x": None,
            "mpp_y": None,
            "objective": objective_f,
            "mpp_source": "missing",
        }

    mpp_x_f = _to_float(props.get("openslide.mpp-x"))
    mpp_y_f = _to_float(props.get("openslide.mpp-y"))

    if mpp_x_f is not None and mpp_y_f is not None:
        mpp_source = "metadata"
    else:
        tiff_mpp_x, tiff_mpp_y = _mpp_from_tiff_resolution(path)
        if tiff_mpp_x is not None:
            mpp_x_f = mpp_x_f if mpp_x_f is not None else tiff_mpp_x
            mpp_y_f = mpp_y_f if mpp_y_f is not None else tiff_mpp_y
            mpp_source = "tiff-resolution"
        elif objective_f is not None and objective_f > 0:
            est = 10.0 / objective_f
            mpp_x_f = mpp_x_f if mpp_x_f is not None else est
            mpp_y_f = mpp_y_f if mpp_y_f is not None else est
            mpp_source = "estimated"
        else:
            mpp_x_f = None
            mpp_y_f = None
            mpp_source = "missing"

    return {
        "width": width,
        "height": height,
        "mpp_x": mpp_x_f,
        "mpp_y": mpp_y_f,
        "objective": objective_f,
        "mpp_source": mpp_source,
    }


# --------------------------------------------------------------------------- #
# 多通道 render context（Batch 3，规格 §6.1/§6.2/§6.3/§7.3）
#
# 颜色/签名/校验算法全部在 slide_render（与 app.py 共用）；本区块只做
# Flask 侧粘合：secret 读取、generation 命名空间、瓦片缓存键、稳定错误映射。
# 日志只记安全标识（safe 名 / fingerprint 前缀 / 通道数 / 错误码），不记
# token 全文与图像内容。
# --------------------------------------------------------------------------- #
_RENDER_SECRET_CACHE = None


def _render_secret() -> str:
    """render_token 签名 secret：与主站 app.py 同一来源。

    优先 SECRET_KEY env；否则读/创建 SHARE_DATA_DIR/flask_secret.key
    （0600，与 flask secret 同文件——同一部署共享 secret，split 部署下本服务
    首个 worker 负责创建）。派生 key 在 slide_render.derive_render_key：
    多 worker / 双进程无共享内存也可互相验证。
    """
    global _RENDER_SECRET_CACHE
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    data_dir = Path(os.environ.get("SHARE_DATA_DIR")
                    or (Path.home() / "svs-viewer" / "share-data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_file = data_dir / "flask_secret.key"
    path_key = str(secret_file)
    cached = _RENDER_SECRET_CACHE
    if cached and cached[0] == path_key:
        return cached[1]

    def _read_or_create_locked():
        if secret_file.is_file():
            try:
                raw = secret_file.read_text(encoding="utf-8").strip()
                if raw:
                    return raw
            except OSError:
                pass
        key = secrets.token_hex(32)
        tmp = secret_file.with_name(secret_file.name + ".tmp")
        tmp.write_text(key, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, secret_file)
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        return key

    try:
        import fcntl

        with open(str(secret_file) + ".lock", "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                key = _read_or_create_locked()
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        key = _read_or_create_locked()
    _RENDER_SECRET_CACHE = (path_key, key)
    return key


def _multichannel_enabled() -> bool:
    """feature flag PATHTOGETHER_MULTICHANNEL_ENABLED（默认关闭，§15.2）。"""
    return slide_render.multichannel_enabled()


def _slide_scope_key(desc_or_name):
    """R-15 缓存/统计/token 的 slide 绑定键（P2 收口）：descriptor →
    slide_id；legacy 名（无行兼容）→ 原名。与主站 app._slide_scope_key 同义。"""
    if isinstance(desc_or_name, slide_store.SlideDescriptor):
        return (desc_or_name.slide_id
                or desc_or_name.legacy_filename
                or desc_or_name.original_filename or "")
    return desc_or_name


def _ctx_scope(safe: str, generation) -> str:
    """统计缓存 scope："<safe>#<generation>"（与主站同一命名空间约定）。

    P2（R-15）：safe 实参为 _slide_scope_key 派生的 slide_id 键。
    """
    return "%s#%s" % (safe, generation)


def _legacy_revision(safe: str) -> str:
    """切片 asset revision（mtime:size，与主站 _legacy_slide_revision 同源；
    P6 §8 收口后仅剩无 descriptor 的防御路径使用）。"""
    try:
        st = os.stat(UPLOAD_DIR / safe)
        return "{}:{}".format(st.st_mtime_ns, st.st_size)
    except OSError:
        return ""


def _share_revision(desc) -> str:
    """分享端 revision 取数（P6 任务 §8 收口）：与主站 _slide_revision 同源
    ——id_bundle 资产=slide_assets 内容 revision（sha256 前缀），签发/验签/
    fingerprint 同值；legacy 布局成员在门禁层已拒（无消费者）。"""
    if isinstance(desc, slide_store.SlideDescriptor) \
            and desc.storage_layout == slide_store.StorageLayout.ID_BUNDLE:
        return desc.revision or ""
    return ""


def _resolve_pair(pair, skey: str, path=None, *, token="", body=None,
                  flag=True, revision=None):
    """在当前借出的 pair 上解析 render context（§6.3；解码前拒绝）。

    返回 (context|None, fingerprint)；context None → native/legacy 路径。
    默认 context 的 (fp, image_mode) 回填 per-generation 缓存（与主站同语义）。
    P2（R-15）：``skey`` 是 slide_id 绑定键；``path`` 为 descriptor 解析后的
    绝对路径（revision 取数源；缺省按旧名拼 UPLOAD_DIR）。
    """
    rev_target = path if path is not None else skey
    gen_scope = _ctx_scope(skey, pair.get("gen"))
    ctx, fp = slide_render.resolve_render_context(
        pair["osr"], safe=skey,
        expected_revision=(revision if revision is not None
                           else _legacy_revision(rev_target)),
        token=token, token_secret=_render_secret(), body=body,
        asset_generation=gen_scope, flag_enabled=flag)
    if ctx is None:
        fp = slide_render.NATIVE_RGB_FINGERPRINT
    elif not token and body is None:
        _default_fp_store(skey, pair.get("gen"), fp,
                          slide_render.image_mode_from_context(ctx))
    return ctx, fp or slide_render.NATIVE_RGB_FINGERPRINT


def _region_view(pair, ctx, fp):
    """按需包装当前借出的 osr（RenderedSlideView 不跨 borrow 缓存，§7.3）。"""
    if ctx is None:
        return pair["osr"]
    return slide_render.RenderedSlideView(pair["osr"], ctx, fingerprint=fp)


def _render_error(e):
    """SlideRenderError / RenderRequestError → 稳定 JSON 契约（§7.4）。"""
    status = getattr(e, "status", None) \
        or slide_render.RENDER_ERROR_HTTP.get(getattr(e, "code", ""), 400)
    return jsonify(error=str(e), code=getattr(e, "code",
                                              "invalid_render_context")), status


def _display_param_error_response(e):
    """viewer 显示参数错误 → 400 invalid_display_profile，no-store（§5.2）。"""
    resp = jsonify(error=str(e), code=getattr(e, "code",
                                              "invalid_display_profile"))
    resp.headers["Cache-Control"] = "no-store"
    return resp, getattr(e, "status", 400)


def _display_conflict_response(e):
    """过时 display_version → 409 display_version_conflict，no-store。"""
    resp = jsonify(error=str(e), code=getattr(e, "code",
                                              "display_version_conflict"))
    resp.headers["Cache-Control"] = "no-store"
    return resp, getattr(e, "status", 409)


def _tile_jpeg_response(data):
    """瓦片/缩略图字节响应（§5.4）：private no-cache + 强 ETag + 条件 304。

    与主站 app.py 同一策略（本函数形状一致，策略在 viewer_display/tile_cache
    共享实现）；304 只在鉴权与版本核验之后到达。
    """
    tag = hashlib.sha256(data).hexdigest()
    if tag in request.if_none_match:
        resp = app.response_class(status=304)
        resp.headers["Cache-Control"] = "private, no-cache"
        resp.headers["ETag"] = '"%s"' % tag
        return resp
    resp = send_file(io.BytesIO(data), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, no-cache"
    resp.headers["ETag"] = '"%s"' % tag
    return resp


def _tile_fp_key(safe: str, generation, fp, level, x, y, fmt="jpeg",
                 spec=None, image_mode="native_rgb"):
    """瓦片缓存键（§7.3 + §3.1/§6.3）：与主站同一形状——encoding_fingerprint
    由同一个规范化 EncodingSpec 派生（键与编码同源；legacy 按运行时
    JPEG_QUALITY 解析）。"""
    if spec is None:
        spec = slide_render.resolve_viewer_encoding(
            image_mode, "tile", None, quality_override=JPEG_QUALITY)
    return (safe, generation, fp, int(level), int(x), int(y), fmt,
            slide_render.viewer_encoding_fingerprint(spec))


def _tile_cache_lookup(safe, generation, fp, level, x, y):
    """legacy 预查不知道 context version：两个编码键都试（与主站同语义）。"""
    for mode in ("native_rgb", "multichannel"):
        hit = _tile_cache_get(_tile_fp_key(
            safe, generation, fp, level, x, y, image_mode=mode))
        if hit is not None:
            return hit
    return None


def _tile_cache_lookup_spec(safe, generation, fp, level, x, y, spec):
    return _tile_cache_get(_tile_fp_key(
        safe, generation, fp, level, x, y, spec=spec))


def _encode_tile_jpeg(tile, ctx, profile_id=None):
    """按解析后的 context 选编码 spec 并编码（§3.1 viewer 三件套）。"""
    image_mode = slide_render.image_mode_from_context(ctx)
    spec = slide_render.resolve_viewer_encoding(
        image_mode, "tile", profile_id, quality_override=JPEG_QUALITY)
    t0 = time.monotonic()
    data, _actual = slide_render.encode_viewer_jpeg(tile, spec)
    _viewer_metrics.observe_encode(spec.profile_id,
                                   (time.monotonic() - t0) * 1000.0)
    return data, spec


def _render_info_fields(safe: str, desc=None, *, path=None) -> dict:
    """分享端 info 的 render additive 字段（§6.1；与主站同一实现）。

    image-transport-upgrade（§5.2）：同一次稳定读取 additive 返回
    ``display`` 对象 + ``display_encoding_v1`` 能力位；分享端有 thumbnail
    端点 → include_thumbnail=True。P2（R-15）：desc 给出时统计 scope/token
    绑定键 = slide_id；revision 按入口文件（mtime:size）。
    P6 运行时退役收口：``path`` 为 descriptor 解析后的绝对路径（句柄与
    revision 的唯一取数源）；无 path 的按名平铺分支已随平铺读取残留复核
    拆除——调用方必须传 path（或 desc 由上游 _require_slide* 保证 id_bundle）。
    """
    if path is None and desc is not None:
        path = slide_storage.resolve_descriptor_path(desc, root=UPLOAD_DIR)
    skey = _slide_scope_key(desc) if desc is not None else safe
    entry = _get_slide(skey, path)

    def _read(pair):
        fields = slide_render.build_render_info(
            pair["osr"],
            asset_revision=(_share_revision(desc) if desc is not None
                            else _legacy_revision(path)),
            asset_generation=_ctx_scope(skey, pair.get("gen")),
            secret=_render_secret(), slide_name=skey,
            flag_enabled=_multichannel_enabled())
        try:
            z_dims = pair.get("dz").level_dimensions
            fields["deepzoom"] = {
                "width": int(z_dims[-1][0]),
                "height": int(z_dims[-1][1]),
                "tile_size": DZ_TILE_SIZE,
                "overlap": DZ_OVERLAP,
                "min_level": 0,
                "max_level": len(z_dims) - 1,
            }
        except Exception:  # noqa: BLE001
            pass
        # flag 关：display 按 native_rgb 提供档位（tile 实际按 native 编码）
        display_fields = dict(fields)
        if not _multichannel_enabled():
            display_fields["image_mode"] = "native_rgb"
        try:
            fields["display"] = viewer_display.build_display_info(
                entry, display_fields, include_thumbnail=True,
                quality_override=JPEG_QUALITY)
            cap = fields.setdefault("server_capability", {})
            cap["display_encoding_v1"] = True
        except Exception:  # noqa: BLE001  显示身份不可得时保持旧 info 语义
            app.logger.warning("[viewer-display] display 信息构建失败",
                               exc_info=True)
        return fields

    return slide_cache.read_stable(entry, _read)[0]


# --------------------------------------------------------------------------- #
# 安全核心：token 与 slide 校验
# --------------------------------------------------------------------------- #
# 设备级访客身份：为每个访问 /s/* 的设备静默分配签名 cookie（svs_visitor）。
# 该设备创建的标注会记录 visitor 的 HMAC 哈希，仅允许同一访客编辑/删除自己新建
# 的标记。API 响应不回传原始 visitor，避免复制 cookie 冒用。
VISITOR_COOKIE = "svs_visitor"
VISITOR_MIG_COOKIE = "svs_visitor_mig"
_VISITOR_COOKIE_PREFIX = "v2."
_VISITOR_STORED_PREFIX = "h1."
# (path, secret_bytes)；路径变化（测试隔离）时自动失效
_visitor_secret_cache = None


def _is_secure():
    """按 SHARE_TLS_CERT 是否配置判定当前部署是否走 HTTPS（cookie secure 用）。"""
    cert = os.environ.get("SHARE_TLS_CERT")
    return bool(cert) and os.path.exists(cert)


def _visitor_data_dir() -> Path:
    d = getattr(share_store, "SHARE_DATA_DIR", None)
    if d:
        return Path(d)
    return Path(os.environ.get("SHARE_DATA_DIR") or (Path.home() / "svs-viewer" / "share-data"))


def _visitor_hmac_secret() -> bytes:
    """HMAC 密钥：SHARE_DATA_DIR/visitor_hmac.key（0600），跨进程复用。

    gunicorn 多 worker 首启与 Flask secret 相同：fcntl 排他锁 + 双检 + 临时文件
    原子替换；进程内按路径缓存，避免每次请求读盘。
    """
    global _visitor_secret_cache
    data_dir = _visitor_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_file = data_dir / "visitor_hmac.key"
    path_key = str(secret_file)
    cached = _visitor_secret_cache
    if cached and cached[0] == path_key:
        return cached[1]

    def _read_or_create_locked():
        if secret_file.is_file():
            try:
                raw = secret_file.read_text(encoding="utf-8").strip()
                if raw:
                    return raw.encode("utf-8")
            except OSError:
                pass
        key = secrets.token_hex(32)
        tmp = secret_file.with_name(secret_file.name + ".tmp")
        tmp.write_text(key, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, secret_file)
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        return key.encode("utf-8")

    try:
        import fcntl
        lock_file = data_dir / "visitor_hmac.lock"
        with open(lock_file, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                key_bytes = _read_or_create_locked()
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        key_bytes = _read_or_create_locked()

    _visitor_secret_cache = (path_key, key_bytes)
    return key_bytes


def _sign_visitor(vid: str) -> str:
    sig = hmac.new(_visitor_hmac_secret(), vid.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    return "%s%s.%s" % (_VISITOR_COOKIE_PREFIX, vid, sig)


def _parse_visitor_cookie(raw):
    """校验签名 cookie，返回 visitor id；非法 cookie 返回 None。

    旧 unsigned cookie 不在这里接受；由 _bind_visitor 结合 svs_visitor_mig
    走「当前 token 有明文证明 → 全局迁移」认领。
    """
    if not raw or not isinstance(raw, str):
        return None
    if not raw.startswith(_VISITOR_COOKIE_PREFIX):
        return None
    rest = raw[len(_VISITOR_COOKIE_PREFIX):]
    vid, sep, sig = rest.partition(".")
    if not sep or not vid or not sig:
        return None
    expected = hmac.new(
        _visitor_hmac_secret(), vid.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    return vid


def _visitor_stored(vid):
    """落库用的 visitor 哈希（不含原始 id）。"""
    if not vid:
        return ""
    digest = hmac.new(
        _visitor_hmac_secret(), vid.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return _VISITOR_STORED_PREFIX + digest


def _visitor_id():
    """当前请求的访客 id（before_request 绑定）；无则 None。"""
    return getattr(g, "visitor_id", None)


def _share_token_from_path(path):
    """从 /s/<token>/... 抽出 share token；无则 None。"""
    if not path:
        return None
    parts = path.split("/")
    if len(parts) >= 3 and parts[1] == "s" and parts[2]:
        return parts[2]
    return None


def _legacy_plaintext(raw):
    """从 cookie 取出升级前的 unsigned visitor id；签名/空值返回 None。"""
    if not raw or not isinstance(raw, str):
        return None
    if raw.startswith(_VISITOR_COOKIE_PREFIX) or raw.startswith(_VISITOR_STORED_PREFIX):
        return None
    return raw


def _reclaim_legacy_visitor(token, plaintext_vid, canonical_vid=None):
    """升级路径：当前 token 的分享仍有效且确有明文匹配时，原子全局迁移。

    canonical_vid：已有合法签名身份时，明文 ROI 迁到该身份的哈希，避免切换
    cookie 丢掉迁移期间新建 ROI 的所有权。缺省则迁到 plaintext_vid 自身。
    share 有效性在存储层与 ROI 改写同一锁/事务内检查（撤销/过期/缺失均失败，
    不会当场迁移）。失败不销毁 legacy 凭据：调用方写入 svs_visitor_mig，之后
    在仍 active 的链接上先到先得认领。
    返回是否认领成功。
    """
    if not token or not plaintext_vid or plaintext_vid.startswith(_VISITOR_COOKIE_PREFIX):
        return False
    target = canonical_vid or plaintext_vid
    if not target or target.startswith(_VISITOR_COOKIE_PREFIX):
        return False
    hashed = _visitor_stored(target)
    if not hashed or hashed == plaintext_vid:
        return False
    try:
        n = share_store.rehash_plaintext_visitors(token, plaintext_vid, hashed)
    except Exception:
        return False
    return n > 0


def _set_visitor_cookie(resp, name, value):
    resp.set_cookie(
        name,
        value,
        path="/s",
        httponly=True,
        samesite="Lax",
        secure=_is_secure(),
    )


@app.before_request
def _bind_visitor():
    """/s/* 绑定访客：签名 cookie 复用；legacy 凭据可在仍有效的 token 上认领。

    已有合法 v2 时它是 canonical：明文 ROI 迁到该身份，不切换 cookie。
    无 v2 才签发 legacy 签名。当场认领失败（含 share 撤销/过期/缺失、/s、
    无匹配 ROI）时签发随机身份，并保留 svs_visitor_mig：无效链接不得立即
    全局迁移，但同一浏览器之后访问仍 active 的链接可先到先得认领。
    """
    path = request.path
    if path != "/s" and not path.startswith("/s/"):
        return
    raw = request.cookies.get(VISITOR_COOKIE)
    mig = request.cookies.get(VISITOR_MIG_COOKIE)
    vid = _parse_visitor_cookie(raw)
    legacy = _legacy_plaintext(raw) or _legacy_plaintext(mig)
    token = _share_token_from_path(path)
    g.visitor_issue_cookie = False
    g.visitor_mig_cookie = None
    g.visitor_mig_clear = False

    if legacy and token and _reclaim_legacy_visitor(token, legacy, canonical_vid=vid):
        # 已有合法 v2：保留该身份，明文 ROI 已迁到其哈希。无 v2 才签发 legacy。
        g.visitor_id = vid or legacy
        g.visitor_issue_cookie = not bool(vid)
        g.visitor_mig_clear = True
        return
    if vid:
        g.visitor_id = vid
        return
    g.visitor_id = secrets.token_urlsafe(16)
    g.visitor_issue_cookie = True
    if legacy:
        g.visitor_mig_cookie = legacy


@app.after_request
def _ensure_visitor_cookie(resp):
    """补发签名访客 cookie。当场认领失败则写入/保留 mig；认领成功才清除。"""
    if getattr(g, "visitor_issue_cookie", False) and getattr(g, "visitor_id", None):
        _set_visitor_cookie(resp, VISITOR_COOKIE, _sign_visitor(g.visitor_id))
    if getattr(g, "visitor_mig_clear", False):
        resp.delete_cookie(VISITOR_MIG_COOKIE, path="/s")
    elif getattr(g, "visitor_mig_cookie", None):
        _set_visitor_cookie(resp, VISITOR_MIG_COOKIE, g.visitor_mig_cookie)
    return resp


def _roi_owned_by(r, visitor):
    """判断某 roi 是否归当前访客所有（用于编辑/删除的归属校验）。

    无 visitor 字段 = 旧数据：按链接级共享，任意访客可编辑（兼容历史行为）。
    新数据存 HMAC 哈希，与当前签名 cookie 的 id 哈希比对。
    """
    v = r.get("visitor") or ""
    if not v:
        return True
    if not visitor:
        return False
    if v.startswith(_VISITOR_STORED_PREFIX):
        return hmac.compare_digest(v, _visitor_stored(visitor))
    # 旧明文 visitor：仅当签名 cookie 的 id 与明文一致（无法用复制的明文伪造签名）
    return hmac.compare_digest(v, visitor)


def _public_roi(r, access_token=None):
    """分享端响应：去掉 visitor，并去掉非本链接的来源分享 token。"""
    import annotation_access
    return annotation_access.public_roi_view(r, access_token=access_token)


def _require_share(token):
    """校验 token 有效，返回 share dict；无效则 404（不泄露信息）。"""
    share = share_store.get_share(token)
    if share is None:
        abort(404, "链接无效或已过期")
    return share


def _fmt_mm(v):
    """把 mm 数值格式化为整数优先、否则一位小数（6 → "6"，6.5 → "6.5"）。"""
    f = float(v)
    if f == int(f):
        return str(int(f))
    # 6.5 这类保留一位
    return ("%.1f" % f).rstrip("0").rstrip(".")


def _share_slide_member(token, slide_id):
    """share_slides(token, slide_id) 成员判定（R-04：ID 关系唯一授权来源）。

    token 有效性/过期/撤销由调用方先经 _require_share（get_share）裁决——
    这里只做成员关系。DB 异常按非成员处理（fail-closed）。
    """
    if not token or not slide_id:
        return False
    try:
        conn = pg_store.connect()
    except Exception:
        app.logger.warning("share_slides 成员查询连接失败（fail-closed）",
                           exc_info=True)
        return False
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM share_slides WHERE token=%s AND slide_id=%s"
                    " LIMIT 1", (token, slide_id))
                return cur.fetchone() is not None
    except Exception:
        app.logger.warning("share_slides 成员查询失败（fail-closed）",
                           exc_info=True)
        return False
    finally:
        conn.close()


def _share_slide_rows(token):
    """分享成员（share_slides ⋈ slides）清单：[(legacy_filename, asset_state,
    slide_id, display_name, original_filename, format_ext)]。

    P1-B2 列表端点数据源（替代遍历 shares.slides JSONB 快照）；按 position
    排序。DB 异常返回 None（调用方 fail-closed 503）。
    P2（前端缺口①）：行携带 slide_id/display_name/original_filename/
    format_ext——分享页前端以 slide_id 为操作键；legacy_filename 为 NULL
    的 id_bundle 成员（P3 起）由调用方走 descriptor 路径。
    """
    try:
        conn = pg_store.connect()
    except Exception:
        app.logger.warning("share_slides 成员清单连接失败", exc_info=True)
        return None
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT s.legacy_filename, s.asset_state, s.slide_id, "
                    "s.display_name, s.original_filename, s.format_ext "
                    "FROM share_slides ss JOIN slides s"
                    " ON s.slide_id = ss.slide_id "
                    "WHERE ss.token=%s ORDER BY ss.position, s.legacy_filename",
                    (token,))
                rows = cur.fetchall()
        return [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]
    except Exception:
        app.logger.warning("share_slides 成员清单查询失败", exc_info=True)
        return None
    finally:
        conn.close()


def _require_slide(share, name):
    """校验 name 属于该 share 且资产可读；403/404 语义不变。

    P1-B2（R-04）：``resolve_legacy_alias``（冻结别名，无文件系统猜测）→
    ``share_slides(token, slide_id)`` 成员 → ``asset_state='ready'`` 门禁
    （staging/legacy 未回填/deleting/deleted/failed 不可读，合同 §4）。
    **shares.slides JSONB 不再参与这里的判定**（兼容快照）。无行/非成员/
    非 ready → 403「无权访问」（与旧口径一致，不泄露差异；行存在且成员、
    文件缺失仍由 _get_slide 404）。返回 SlideDescriptor（legacy_filename 即
    净化后的 name；文件读走 slide_storage.resolve_descriptor_path）。
    P6 运行时退役：legacy 布局行（待迁移/隔离）同 403——读路径只认
    id_bundle，不泄露存在性差异。
    """
    safe = _sanitize_name(name)
    if not safe or safe != name:
        abort(403, "无权访问")
    try:
        desc = slide_store.resolve_legacy_alias(safe)
    except Exception:
        app.logger.warning("分享通道 legacy 解析失败（fail-closed）：%s",
                           safe, exc_info=True)
        desc = None
    if (desc is None
            or not _slide_member_readable(desc)
            or not _share_slide_member(share.get("token"), desc.slide_id)):
        abort(403, "无权访问")
    return desc


def _slide_member_readable(desc):
    """分享端成员可读性：ready 且 id_bundle（P6 运行时退役——legacy 布局行
    = 待迁移/隔离，403 同「无权」口径，不泄露存在性）。"""
    return (desc.asset_state == slide_store.SlideState.READY
            and desc.storage_layout == slide_store.StorageLayout.ID_BUNDLE)


def _require_slide_by_id(share, slide_id):
    """校验 slide_id 属于该 share 且资产可读（P2，合同 §4）；403/404 同旧口径。

    resolve_slide_id（校验存在性）→ share_slides(token, slide_id) 成员 →
    asset_state='ready' 门禁。未知 ID → 403「无权访问」（与名通道一致，
    不泄露差异）；行存在且成员、文件缺失仍由 _get_slide 404。
    P6 运行时退役：legacy 布局行同 403（读路径只认 id_bundle）。
    """
    sid = (slide_id or "").strip() if isinstance(slide_id, str) else ""
    if not sid:
        abort(403, "无权访问")
    try:
        desc = slide_store.resolve_slide_id(sid)
    except Exception:
        app.logger.warning("分享通道 slide_id 解析失败（fail-closed）：%s",
                           sid, exc_info=True)
        desc = None
    if (desc is None
            or not _slide_member_readable(desc)
            or not _share_slide_member(share.get("token"), desc.slide_id)):
        abort(403, "无权访问")
    return desc


def _share_has_annotate(share):
    """判断分享是否含 annotate 权限（docs §5.4 权限三档）。

    旧分享无 permissions 字段 → 默认 view+annotate（严格等价旧行为）。
    """
    perms = share.get("permissions")
    if not isinstance(perms, list) or not perms:
        return True  # 旧链接默认可标注
    return share_store.PERMISSION_ANNOTATE in perms


# --------------------------------------------------------------------------- #
# 升级 C（§6.4）：分享矩形策略（rect_policy）与真实几何校验
# --------------------------------------------------------------------------- #
#: preset_only 尺寸复核容差（比例）：服务端按可信 MPP 反算 mm 与自报 size_mm
#: 比较，四舍五入像素误差在该容差内接受。
_PRESET_MM_TOLERANCE = 0.02


def _slide_dims_and_mpp(safe, path=None):
    """读切片 level-0 尺寸与 MPP（读元数据，不解码像素）。

    返回 (width, height, mpp_x, mpp_y)；任一不可读以 None 占位。
    P2（缺口②）：path 显式给出时按解析后路径读（id_bundle 成员无 legacy
    名；缓存键随之用路径字符串），缺省保持 UPLOAD_DIR / safe 旧口径。
    """
    if path is None:
        path = UPLOAD_DIR / safe
        key = safe
    else:
        key = str(path)
    entry = _get_slide(key, path)
    try:
        def _read(pair):
            meta = _read_metadata(pair["osr"], path)
            return (pair["osr"].dimensions, meta.get("mpp_x"),
                    meta.get("mpp_y"))

        (dims, mpp_x, mpp_y), _gen = slide_cache.read_stable(entry, _read)
        def _finite(v):
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if f == f and f not in (float("inf"), float("-inf")) \
                and f > 0 else None
        return (dims[0], dims[1], _finite(mpp_x), _finite(mpp_y))
    except Exception:  # noqa: BLE001 - 读失败按缺尺寸/MPP 降级
        return None, None, None, None


def _rect_policy_of(share):
    """分享 rect_policy（§6.4）：旧分享缺字段一律 preset_only。"""
    return share_shared._share_rect_policy(share)


def _reject_preset_rect(share, safe, geom, path=None):
    """preset_only 分享的 rect 新增校验（§6.4，不能只查自报 size_mm=6）。

    要求：
      1. size_mm ∈ 该分享 roi_sizes 子集；
      2. 几何必须是正方形（成对 w/h 相等，或旧 side_px）；
      3. 切片有可信 MPP，且 side_px 反算的物理边长与 size_mm 一致（±2%）
         ——防「自报 6mm 实画 60mm」绕过预设子集。

    返回 None（通过）或 (message, http_status)。
    """
    allowed = share.get("roi_sizes") or list(share_store.DEFAULT_ROI_SIZES)
    size_mm_v = geom.get("size_mm")
    try:
        smm = float(size_mm_v)
    except (TypeError, ValueError):
        smm = None
    if smm is None or smm not in allowed:
        label_str = " / ".join(_fmt_mm(v) for v in allowed)
        return ("本次分享仅允许 " + label_str + " mm 标记", 403)
    eff_w = geom.get("w")
    eff_h = geom.get("h")
    if eff_w is None or eff_h is None:
        side = geom.get("side_px")
        eff_w = side
        eff_h = side
    return _reject_preset_rect_mm(share, safe, eff_w, eff_h, declared_mm=smm,
                                  path=path)


def _reject_preset_rect_mm(share, safe, w, h, declared_mm=None, path=None):
    """preset_only 的真实几何校验：正方形 + 可信 MPP 反算 mm ∈ roi_sizes。

    编辑路径用本函数（无需客户端 size_mm；按真实几何反算）。declared_mm
    提供时（新增路径）还要求反算值与自报值一致。
    返回 None（通过）或 (message, http_status)。
    """
    try:
        w_i, h_i = int(w), int(h)
    except (TypeError, ValueError):
        return ("矩形几何非法", 400)
    if w_i != h_i:
        return ("本次分享仅允许正方形预设标记（6/6.5mm），不支持自定义矩形", 403)
    allowed = [float(v) for v in
               (share.get("roi_sizes") or list(share_store.DEFAULT_ROI_SIZES))]
    label_str = " / ".join(_fmt_mm(v) for v in allowed)
    _w, _h, mpp_x, _mpp_y = _slide_dims_and_mpp(safe, path=path)
    if not mpp_x:
        return ("该切片缺少可信 MPP，无法核验预设尺寸；请在主站校准后重试", 403)
    real_mm = w_i * mpp_x / 1000.0
    if declared_mm is not None:
        smm = float(declared_mm)
        if abs(real_mm - smm) > max(smm * _PRESET_MM_TOLERANCE, 0.01):
            return ("标记实际尺寸（%.2fmm）与所选预设（%s mm）不符"
                    % (real_mm, label_str), 403)
        return None
    for v in allowed:
        if abs(real_mm - v) <= max(v * _PRESET_MM_TOLERANCE, 0.01):
            return None
    return ("标记实际尺寸（%.2fmm）不在本次分享允许的预设（%s mm）内"
            % (real_mm, label_str), 403)


def _reject_rect_out_of_bounds(safe, x, y, w, h, path=None):
    """rect 写入的真实切片边界校验（§6.3-4，分享端路径入口）。None 或 message。

    P2（缺口②）：path 显式给出时按解析后路径读（id_bundle 成员）。"""
    width, height, _mpp_x, _mpp_y = _slide_dims_and_mpp(safe, path=path)
    return share_shared._validate_rect_bounds(x, y, w, h, width, height)


# --------------------------------------------------------------------------- #
# Stage 3c-2：分享访问日志 + 归档只读（docs §5.4/§v1.5）
# --------------------------------------------------------------------------- #
# 分享访问日志：/s/<token> 页面加载与 /s/<token>/api/* 关键调用记 audit
# （action=share.access，detail 带 visitor 与 IP 后两段脱敏）。
# 为防每请求一条太密，按「同 token + 同 visitor」做 5 分钟窗口去重：内存 dict
# {token+"|"+visitor: last_ts}，窗口内同键不再重复记。简单可靠，无需持久化状态。
_SHARE_ACCESS_WINDOW = 300.0
_share_access_last: dict = {}
_share_access_lock = threading.Lock()


def _mask_ip(ip: str) -> str:
    """IP 脱敏：保留前两段（IPv4）或前 6 字符（IPv6），后段用 *。"""
    if not ip:
        return ""
    if ":" in ip:
        return ip[:6] + "*"
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:2]) + ".*.*"
    return "*"


def _log_share_access(token, detail=None):
    """best-effort 记一条 share.access 审计事件（5 分钟窗口去重）。

    调用方无需 try（record_audit 自吞失败）。去重键 = token + visitor。
    """
    visitor = _visitor_id() or ""
    key = token + "|" + visitor
    now = time.time()
    with _share_access_lock:
        last = _share_access_last.get(key)
        if last is not None and (now - last) < _SHARE_ACCESS_WINDOW:
            return
        _share_access_last[key] = now
    d = dict(detail or {})
    d["visitor"] = visitor[:8] if visitor else ""
    d["ip"] = _mask_ip(request.remote_addr or "")
    share_store.record_audit(
        action="share.access", target_type="share", target_id=token,
        detail=d,
    )


def _archived_slide_names():
    """属于归档项目的切片名集合（归档只读判定）。"""
    return share_store.archived_slide_names()


def _reject_archived_slide(share, name, slide_id=None):
    """归档项目内切片只读：guest 亦不可在归档项目内标注/评论。命中 → True。

    P2（缺口②）：id_bundle 成员无 legacy 名——name 为 None 时按 slide_id
    查 project_slides 双写列（与 app.py `_slide_id_archived` 同语义）。
    """
    if name is not None:
        return name in _archived_slide_names()
    if not slide_id:
        return False
    try:
        conn = pg_store.connect()
    except Exception:  # noqa: BLE001 - 查询失败按未归档（与名通道容错一致）
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM project_slides ps JOIN projects p "
                "ON p.project_id = ps.project_id "
                "WHERE ps.slide_id=%s AND p.archived LIMIT 1", (slide_id,))
            return cur.fetchone() is not None
    except Exception:  # noqa: BLE001
        return False
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@app.errorhandler(404)
def _handle_404(e):
    return "链接无效或已过期", 404


@app.errorhandler(403)
def _handle_403(e):
    return "无权访问", 403


# --------------------------------------------------------------------------- #
# 存储层 fail-closed（review G1）：损坏/不可用一律 503，绝不映射成 404 空库
# --------------------------------------------------------------------------- #
def _share_store_error_response(e):
    """把 ShareStoreCorrupt/ShareStoreUnavailable 映射为稳定 503 JSON。"""
    code = ("share_store_corrupt"
            if isinstance(e, share_store.ShareStoreCorrupt)
            else "share_store_unavailable")
    app.logger.error("share 存储不可用（%s）：%s", code, e)
    return jsonify(error=code), 503


@app.errorhandler(share_store.ShareStoreCorrupt)
def _handle_share_store_corrupt(e):
    return _share_store_error_response(e)


@app.errorhandler(share_store.ShareStoreUnavailable)
def _handle_share_store_unavailable(e):
    return _share_store_error_response(e)


@app.route("/s/healthz")
def share_healthz():
    """分享服务就绪探针：存储不可读/损坏 → 503（ready 失败，不对外服务空库）。

    只做只读 probe（不迁移、不写数据文件）。combined 部署下主站路径由
    admin app 处理，本端点挂在 /s/ 前缀下供 share worker 单独探活。
    """
    try:
        share_store.probe_readable()
    except share_store.ShareStoreCorrupt as e:
        return jsonify(error="share_store_corrupt", detail=str(e)), 503
    except share_store.ShareStoreUnavailable as e:
        return jsonify(error="share_store_unavailable", detail=str(e)), 503
    return jsonify(status="ok"), 200


@app.route("/")
def index():
    return "链接无效或已过期", 404


@app.route("/s/")
@app.route("/s")
def s_root():
    return "链接无效或已过期", 404


@app.route("/s/<token>")
def share_page(token):
    share = _require_share(token)
    _log_share_access(token, detail={"via": "page"})
    resp = make_response(render_template("share.html", token=token))
    # 入口 HTML 可重新验证（§5.3）：普通刷新必须取得新 UI
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/s/<token>/api/slides")
def share_slides(token):
    share = _require_share(token)
    # P1-B2（R-04）：成员清单来自 share_slides ID 关系（不再遍历 shares.slides
    # JSONB 快照）；读取该清单失败 → 503 fail-closed（不回退 JSONB 猜测）
    member_rows = _share_slide_rows(token)
    if member_rows is None:
        return jsonify(error="share_store_unavailable"), 503
    all_meta = share_store.get_all_slide_meta()
    items = []
    for name, _state, slide_id, display_name, original_filename, format_ext in member_rows:
        # P2（前端缺口①）：成员项携带 slide_id/display_name/original_filename/
        # format_ext——分享页前端以 slide_id 为操作键；name 为 legacy 别名
        # 快照（冻结别名仅展示）。
        # P6 运行时退役 + share_server 残留复核收口：路径**一律按 slide_id
        # 解析 descriptor**（resolve_descriptor_path 只认 id_bundle）——旧
        # 「有 legacy_filename 即平铺 stat」分支拆除（对已迁移行会指错旧位、
        # 对未迁移 legacy 行构成物理读取旁路）；legacy 布局成员 = 待迁移/
        # 隔离，exists=False（不泄露存在性）。
        safe = _sanitize_name(name) if name else None
        path = None
        desc0 = None
        try:
            desc0 = slide_store.resolve_slide_id(slide_id)
            if desc0 is not None \
                    and desc0.storage_layout == slide_store.StorageLayout.ID_BUNDLE:
                path = slide_storage.resolve_descriptor_path(
                    desc0, root=UPLOAD_DIR)
        except Exception:
            path = None
            desc0 = None
        cache_key = slide_id
        info = {"name": safe,
                "slide_id": slide_id,
                "display_name": display_name or "",
                "original_filename": original_filename,
                "format_ext": format_ext,
                "exists": bool(path is not None and path.is_file())}
        sm = all_meta.get(safe, {}) if safe else {}
        info["alias"] = sm.get("alias", "") or (display_name or "")
        info["note"] = sm.get("note", "")
        if path is not None and path.is_file():
            def _read_meta():
                entry = _get_slide(cache_key, path)
                with slide_cache.borrow_pair(entry) as pair:
                    return _read_metadata(pair["osr"], path)
            try:
                meta = slide_cache.cached_read_metadata(cache_key, path, _read_meta)
                info.update(meta)
            except Exception as e:
                info.update({
                    "width": None, "height": None,
                    "mpp_x": None, "mpp_y": None,
                    "mpp_source": "missing",
                    "error": str(getattr(e, "description", e)),
                })
            else:
                # Batch 3（§6.1）：render additive 字段（flag 开才带通道面板；
                # 打开失败的条目保持 error 分支不加，单个失败不阻塞整个列表）。
                # P6 收口：render 通道以 slide_id 为键、路径按 descriptor
                # 解析（旧按名分支随平铺读取残留复核拆除）。
                try:
                    info.update(_render_info_fields(cache_key, desc0,
                                                    path=path))
                except slide_cache.SlideFileChanged:
                    pass
                except (slide_render.RenderRequestError,
                        slide_io.SlideRenderError) as e:
                    info["render_error_code"] = getattr(
                        e, "code", "invalid_render_context")
        else:
            info.update({
                "width": None, "height": None,
                "mpp_x": None, "mpp_y": None,
                "mpp_source": "missing",
                "error": "文件不存在",
            })
        items.append(info)
    return jsonify(items)


@app.route("/s/<token>/api/config")
def share_config(token):
    """返回本次分享的配置（矩形标记允许的尺寸子集）。

    先 _require_share：无效 token → 404，不泄露信息。
    旧分享无 roi_sizes 字段时默认两者皆可。
    """
    share = _require_share(token)
    return jsonify({
        "roi_sizes": share.get("roi_sizes") or list(share_store.DEFAULT_ROI_SIZES),
        # 升级 C（§6.4）：矩形策略档位；旧分享缺字段按 preset_only 解释
        "rect_policy": share_shared._share_rect_policy(share),
    })


@app.errorhandler(slide_cache.SlideFileChanged)
def _handle_slide_file_changed(e):
    """读取期间切片被连续替换（重试后仍变）：丢弃结果，返回可重试 503。"""
    app.logger.warning("切片读取期间文件被连续替换：%s", e)
    return jsonify(error="slide_file_changed"), 503


def _share_dzi_impl(token, desc, files_url):
    """DZI XML 共用实现（P2，合同 §4）：Url 指向调用方通道的 tiles 前缀。"""
    safe = desc.legacy_filename
    entry = _get_slide(safe, slide_storage.resolve_descriptor_path(
        desc, root=UPLOAD_DIR))
    # 尺寸按代读取（review G3）：文件换名重传后 dims 必须来自新代句柄
    (width, height), _gen = slide_cache.read_stable(
        entry, lambda pair: pair["dz"].level_dimensions[-1])

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" '
        f'Url="{files_url}" Format="jpeg" '
        f'Overlap="{DZ_OVERLAP}" TileSize="{DZ_TILE_SIZE}">'
        f'<Size Width="{width}" Height="{height}"/>'
        "</Image>"
    )
    resp = Response(xml, mimetype="application/xml")
    # DZI 元数据短期可变（重传/换切片后尺寸会变），用短缓存
    resp.headers["Cache-Control"] = "max-age=60"
    return resp


@app.route("/s/<token>/api/slide/<name>.dzi")
def share_slide_dzi(token, name):
    share = _require_share(token)
    desc = _require_slide(share, name)
    safe = desc.legacy_filename
    return _share_dzi_impl(token, desc,
                           "/s/%s/api/slide/%s_files/" % (token, safe))


@app.route("/s/<token>/api/slides/<slide_id>/dzi")
def share_slide_dzi_by_id(token, slide_id):
    """分享端 ID 原生 DZI（P2，合同 §4）：瓦片 URL 指向 /tiles/。"""
    share = _require_share(token)
    desc = _require_slide_by_id(share, slide_id)
    return _share_dzi_impl(token, desc,
                           "/s/%s/api/slides/%s/tiles/" % (token, slide_id))


@app.route("/s/<token>/api/slide/<name>/info")
def share_slide_info(token, name):
    """分享端单切片 info（Batch 3 additive）：render 字段与主站同一实现。

    P1-B2：成员判定/状态门禁在 _require_slide（share_slides ID 关系）；
    文件读走 resolve_descriptor_path。
    """
    share = _require_share(token)
    desc = _require_slide(share, name)
    return _share_info_impl(desc)


@app.route("/s/<token>/api/slides/<slide_id>/info")
def share_slide_info_by_id(token, slide_id):
    """分享端 ID 原生 info（P2，合同 §4）。"""
    share = _require_share(token)
    desc = _require_slide_by_id(share, slide_id)
    return _share_info_impl(desc)


def _share_info_impl(desc):
    """分享端 info 共用实现（P2，合同 §4）。

    P6 收口：revision/render 字段的取数源统一为 descriptor 解析后的绝对
    路径（mtime:size 同语义；_require_slide* 已保证 id_bundle——legacy 布局
    成员在门禁层 403，不进入本实现）。
    """
    safe = desc.legacy_filename
    path = slide_storage.resolve_descriptor_path(desc, root=UPLOAD_DIR)
    info = {"name": safe, "exists": path.is_file(),
            "slide_id": desc.slide_id,
            "original_filename": desc.original_filename,
            "display_name": desc.display_name or safe,
            "format_ext": desc.format_ext}
    sm = share_store.get_slide_meta_full(safe)
    info["alias"] = sm.get("alias", "")
    info["note"] = sm.get("note", "")
    if not path.is_file():
        info["error"] = "文件不存在"
        return jsonify(info)
    try:
        entry = _get_slide(safe, path)
        with slide_cache.borrow_pair(entry) as pair:
            info.update(_read_metadata(pair["osr"], path))
    except Exception as e:  # noqa: BLE001  与列表端点同形
        info.update({
            "width": None, "height": None,
            "mpp_x": None, "mpp_y": None,
            "mpp_source": "missing",
            "error": str(getattr(e, "description", e)),
        })
        return jsonify(info)
    try:
        info.update(_render_info_fields(safe, desc, path=path))
    except slide_cache.SlideFileChanged:
        return jsonify(error="slide_file_changed",
                       code="slide_file_changed"), 503
    except (slide_render.RenderRequestError, slide_io.SlideRenderError) as e:
        return _render_error(e)
    return jsonify(info)


@app.route("/s/<token>/api/slide/<name>/render-context", methods=["POST"])
def share_slide_render_context(token, name):
    """分享端渲染上下文规范化（§6.2，不落库）：share token 先行鉴权。"""
    share = _require_share(token)
    desc = _require_slide(share, name)
    safe = desc.legacy_filename
    # P2（R-15）：scope/token 绑定键 = slide_id；revision 按入口文件取数
    skey = _slide_scope_key(desc)
    if not _multichannel_enabled():
        return jsonify(error="多通道渲染未启用",
                       code="multichannel_disabled"), 403
    body = request.get_json(silent=True)
    path = slide_storage.resolve_descriptor_path(desc, root=UPLOAD_DIR)
    entry = _get_slide(safe, path)

    revision = _share_revision(desc)

    def _build(pair):
        canonical, fp = slide_render.resolve_render_context(
            pair["osr"], safe=skey, expected_revision=revision,
            body=body if isinstance(body, dict) else {},
            asset_generation=_ctx_scope(skey, pair.get("gen")),
            flag_enabled=True)
        tok = slide_render.issue_render_token(
            canonical, fp, revision, _render_secret(),
            slide=skey)
        # 自定义 context 的显示身份（§5.2 additive）
        display_versions = None
        try:
            display_versions = \
                viewer_display.display_versions_for_fingerprint(
                    entry, fp,
                    slide_render.image_mode_from_context(canonical),
                    quality_override=JPEG_QUALITY)
        except Exception:  # noqa: BLE001  additive 失败不阻塞规范化
            display_versions = None
        return canonical, fp, tok, display_versions

    try:
        canonical, fp, tok, display_versions = \
            slide_cache.read_stable(entry, _build)[0]
    except (slide_render.RenderRequestError, slide_io.SlideRenderError) as e:
        return _render_error(e)
    app.logger.info("[render-context] share slide=%s fp=%s channels=%d",
                    safe, fp[:8], len(canonical.get("active_channels") or []))
    payload = {
        # 与 info.default_render_context 同构：canonical + 内嵌 fingerprint
        "render_context": dict(canonical, fingerprint=fp),
        "render_context_fingerprint": fp,
        "render_token": tok,
        "asset_revision": revision,
        "warnings": [],
    }
    if display_versions:
        payload["display_versions"] = display_versions
    return jsonify(payload)


@app.route("/s/<token>/api/slides/<slide_id>/render-context", methods=["POST"])
def share_slide_render_context_by_id(token, slide_id):
    """分享端 render-context 的 ID 原生路由（P2 前端缺口④，合同 §4）。

    与按名端点同语义（成员判定 + ready 门禁 + 共用规范化实现）；scope/token
    绑定键本来就是 slide_id（R-15）。P6 收口：revision 统一按 descriptor
    解析后路径取 mtime:size（legacy 布局成员在 _require_slide_by_id 已 403，
    不进入本端点——按名取数的旧口径随之无消费者）。
    """
    share = _require_share(token)
    desc = _require_slide_by_id(share, slide_id)
    safe = desc.legacy_filename
    skey = _slide_scope_key(desc)
    if not _multichannel_enabled():
        return jsonify(error="多通道渲染未启用",
                       code="multichannel_disabled"), 403
    body = request.get_json(silent=True)
    path = slide_storage.resolve_descriptor_path(desc, root=UPLOAD_DIR)
    entry = _get_slide(safe, path)
    revision = _share_revision(desc)

    def _build(pair):
        canonical, fp = slide_render.resolve_render_context(
            pair["osr"], safe=skey, expected_revision=revision,
            body=body if isinstance(body, dict) else {},
            asset_generation=_ctx_scope(skey, pair.get("gen")),
            flag_enabled=True)
        tok = slide_render.issue_render_token(
            canonical, fp, revision, _render_secret(), slide=skey)
        display_versions = None
        try:
            display_versions = \
                viewer_display.display_versions_for_fingerprint(
                    entry, fp,
                    slide_render.image_mode_from_context(canonical),
                    quality_override=JPEG_QUALITY)
        except Exception:  # noqa: BLE001  additive 失败不阻塞规范化
            display_versions = None
        return canonical, fp, tok, display_versions

    try:
        canonical, fp, tok, display_versions = \
            slide_cache.read_stable(entry, _build)[0]
    except (slide_render.RenderRequestError, slide_io.SlideRenderError) as e:
        return _render_error(e)
    app.logger.info("[render-context] share slide=%s fp=%s channels=%d",
                    skey, fp[:8], len(canonical.get("active_channels") or []))
    payload = {
        "render_context": dict(canonical, fingerprint=fp),
        "render_context_fingerprint": fp,
        "render_token": tok,
        "asset_revision": revision,
        "warnings": [],
    }
    if display_versions:
        payload["display_versions"] = display_versions
    return jsonify(payload)


@app.route("/s/<token>/api/slide/<name>_files/<int:level>/<int:x>_<int:y>.jpeg")
def share_slide_tile(token, name, level, x, y):
    """返回 Deep Zoom 单张瓦片 JPEG（512×512，LRU+TTL 缓存）。

    review G3（CACHE-1）：瓦片缓存键含 generation——同名重传/原地改写后旧代
    键不再命中，generation 由 slide_cache 的文件签名统一驱动。
    Batch 3（§6.3/§7.3）：接受 ``?render=<token>``。
    image-transport-upgrade（§5.2/§5.4/§6）：additive 版本化 URL
    ``?profile=<id>&dv=<display_version>``（成对、白名单、409、错误 no-store）；
    缓存键含 encoding_fingerprint（键与编码同源）；private no-cache + 强
    ETag（304 在鉴权与版本核验之后）；single-flight；字节预算 LRU。
    旧 URL（无 profile/dv）保持既有编码语义。
    """
    share = _require_share(token)
    desc = _require_slide(share, name)
    return _share_tile_impl(desc, level, x, y)


@app.route("/s/<token>/api/slides/<slide_id>/tiles/<int:level>/<int:x>_<int:y>.jpeg")
def share_slide_tile_by_id(token, slide_id, level, x, y):
    """分享端 ID 原生瓦片（P2，合同 §4）。"""
    share = _require_share(token)
    desc = _require_slide_by_id(share, slide_id)
    return _share_tile_impl(desc, level, x, y)


def _share_tile_impl(desc, level, x, y):
    """瓦片 JPEG 共用实现（P2，合同 §4/R-15：键/token 绑定按 slide_id）。"""
    safe = desc.legacy_filename
    skey = _slide_scope_key(desc)
    path = slide_storage.resolve_descriptor_path(desc, root=UPLOAD_DIR)
    entry = _get_slide(safe, path)
    render_tok = request.args.get("render") or ""
    flag = _multichannel_enabled()

    try:
        req_profile, req_dv = viewer_display.parse_display_params(
            request.args, "tile")
    except viewer_display.DisplayParamError as e:
        _viewer_metrics.observe_status("400")
        return _display_param_error_response(e)

    token_payload = None
    if flag and render_tok:
        payload = slide_render.verify_render_token(render_tok, _render_secret())
        if payload is not None and payload.get("slide") in ("", skey) \
                and payload.get("rev") == _share_revision(desc):
            token_payload = payload

    gen = slide_cache.refresh_generation(entry)
    fp_pre = None
    mode_pre = None
    if not flag:
        fp_pre, mode_pre = slide_render.NATIVE_RGB_FINGERPRINT, "native_rgb"
    elif token_payload is not None:
        fp_pre = token_payload.get("fp")
        mode_pre = "multichannel" \
            if (token_payload.get("ctx") or {}).get("version") \
            == slide_render.CONTEXT_VERSION_MULTICHANNEL else "native_rgb"
    else:
        cached_default = _default_fp_cached(skey, gen)
        if cached_default is not None:
            fp_pre, mode_pre = cached_default

    pre_spec = None
    if req_profile is not None and mode_pre is not None:
        try:
            pre_spec = slide_render.resolve_viewer_encoding(
                mode_pre, "tile", req_profile, quality_override=JPEG_QUALITY)
        except slide_render.RenderRequestError as e:
            _viewer_metrics.observe_status("400")
            return _render_error(e)

    if fp_pre is not None and (pre_spec is not None or req_profile is None):
        cached = _tile_cache_lookup_spec(
            skey, gen, fp_pre, level, x, y, pre_spec) \
            if pre_spec is not None \
            else _tile_cache_lookup(skey, gen, fp_pre, level, x, y)
        if cached is not None and req_dv is not None and pre_spec is not None:
            try:
                cur_dv = viewer_display.compute_display_version(
                    entry, fp_pre, pre_spec)
            except slide_cache.SlideFileChanged:
                cached = None
            else:
                if cur_dv != req_dv:
                    _viewer_metrics.observe_status("409")
                    return _display_conflict_response(
                        viewer_display.DisplayVersionConflict())
                if not slide_cache.generation_is_current(entry, gen):
                    cached = None
        if cached is not None:
            _viewer_metrics.observe_response(
                mode_pre or "native_rgb", pre_spec.profile_id
                if pre_spec is not None else "legacy", len(cached))
            _viewer_metrics.observe_status("200")
            return _tile_jpeg_response(cached)

    flight_key = (skey, gen, fp_pre or "", int(level), int(x), int(y),
                  req_profile or "legacy", "tile")

    def _decode(pair):
        ctx, fp = _resolve_pair(pair, skey, path=path,
                                token=render_tok, flag=flag,
                                revision=_share_revision(desc))
        cur_mode = slide_render.image_mode_from_context(ctx)
        # spec/版本核验在昂贵合成之前（§4.8）
        try:
            cur_spec = slide_render.resolve_viewer_encoding(
                cur_mode, "tile", req_profile,
                quality_override=JPEG_QUALITY) \
                if req_profile is not None else None
        except slide_render.RenderRequestError:
            raise
        if req_dv is not None:
            cur_dv = viewer_display.compute_display_version(
                entry, fp, cur_spec or slide_render.resolve_viewer_encoding(
                    cur_mode, "tile", None, quality_override=JPEG_QUALITY))
            if cur_dv != req_dv:
                raise viewer_display.DisplayVersionConflict()
        if ctx is None:
            tile = pair["dz"].get_tile(level, (x, y))
        else:
            view = slide_render.RenderedSlideView(pair["osr"], ctx,
                                                  fingerprint=fp)
            dzg = DeepZoomGenerator(view, tile_size=DZ_TILE_SIZE,
                                    overlap=DZ_OVERLAP, limit_bounds=True)
            tile = dzg.get_tile(level, (x, y))
        if tile.mode != "RGB":
            tile = tile.convert("RGB")
        return tile, fp, ctx

    def _produce():
        # 按代读取：读取前后签名变化则换代重读一次（SlideFileChanged → 503）
        (tile, fp, ctx), tile_gen = slide_cache.read_stable(entry, _decode)
        t0 = time.monotonic()
        data, enc_spec = _encode_tile_jpeg(tile, ctx,
                                           profile_id=req_profile)
        _viewer_metrics.observe_decode((time.monotonic() - t0) * 1000.0)
        return data, enc_spec, fp, ctx, tile_gen

    try:
        data, enc_spec, fp, ctx, tile_gen = _tile_single_flight.run(
            flight_key, _produce)
    except viewer_display.DisplayVersionConflict as e:
        _viewer_metrics.observe_status("409")
        return _display_conflict_response(e)
    except (slide_render.RenderRequestError,
            slide_io.SlideRenderError) as e:
        _viewer_metrics.observe_status(
            "400" if getattr(e, "status", 400) < 500 else "5xx")
        if getattr(e, "code", "") == "invalid_display_profile":
            return _display_param_error_response(e)  # 错误响应 no-store（§5.2）
        return _render_error(e)
    except tile_cache.SingleFlightTimeout:
        _viewer_metrics.observe_status("5xx")
        return jsonify(error="瓦片生成排队超时，请重试",
                       code="tile_single_flight_timeout"), 503
    key = _tile_fp_key(skey, tile_gen, fp, level, x, y, spec=enc_spec)
    _tile_cache_put(key, data)
    mode = slide_render.image_mode_from_context(ctx)
    _viewer_metrics.observe_response(mode, enc_spec.profile_id, len(data))
    _viewer_metrics.observe_status("200")
    return _tile_jpeg_response(data)


@app.route("/s/<token>/api/slide/<name>/crop")
def share_slide_crop(token, name):
    """crop PNG 导出（分享端）。

    P0-A §3.5：与主站 /api/slide/<name>/crop 共用 crop_guard（同一实现，
    防两份逻辑漂移）。read_region 之前按实际 w×h 过三道闸：
    像素硬闸 413 / 每分钟像素预算 429（按 share capability 即 token 计）/
    并发闸 429；任何解码前拒绝。分享端无需登录，闸主体是 token。
    Batch 3（§6.3/§7.4）：接受 ``?render=<token>``；像素预算按真实成本×
    启用通道数计；下载文件名加入 render fingerprint 前 8 位（§4.4）。
    升级 C（§6.3-5，E06 收窄口径）：本端点不受 roi_sizes 约束（roi_sizes
    只管 ROI 写入）；支持 ``?x&y&w&h`` 通用矩形——输出 PNG 尺寸精确等于
    请求合法 w/h（不 clamp）；``size`` 与 ``w/h`` 混用冲突 400；旧 ``size``
    走兼容正方形分支（保留 clamp 语义）。
    """
    share = _require_share(token)
    desc = _require_slide(share, name)
    return _share_crop_impl(desc, token)


@app.route("/s/<token>/api/slides/<slide_id>/crop")
def share_slide_crop_by_id(token, slide_id):
    """分享端 ID 原生 crop（P2，合同 §4）。"""
    share = _require_share(token)
    desc = _require_slide_by_id(share, slide_id)
    return _share_crop_impl(desc, token)


def _share_crop_impl(desc, token):
    """分享端 crop 共用实现（P2，合同 §4；R-19 下载名走 original_filename；
    P6 收口：revision 按解析后路径）。"""
    safe = desc.legacy_filename
    skey = _slide_scope_key(desc)
    path = slide_storage.resolve_descriptor_path(desc, root=UPLOAD_DIR)
    entry = _get_slide(safe, path)
    render_tok = request.args.get("render") or ""

    def _parse_int(key):
        raw = request.args.get(key)
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    x = _parse_int("x")
    y = _parse_int("y")
    size = _parse_int("size")
    w = _parse_int("w")
    h = _parse_int("h")
    if x is None or y is None:
        return jsonify(error="x/y 参数需为整数"), 400
    if x < 0 or y < 0:
        return jsonify(error="参数越界（0<=x,y）"), 400
    has_size = size is not None
    has_pair = w is not None or h is not None
    if has_size and has_pair:
        return jsonify(error="size 与 w/h 参数冲突，二者只能选其一"), 400
    if has_pair:
        if w is None or h is None:
            return jsonify(error="w/h 参数需成对提供"), 400
        if w < 1 or h < 1 or w > 40000 or h > 40000:
            return jsonify(error="参数越界（0<=x,y，1<=w,h<=40000）"), 400
        square = (w == h)
    else:
        if size is None:
            return jsonify(error="缺少 size 或 w/h 参数"), 400
        if size <= 0 or size > 40000:
            return jsonify(error="参数越界（0<=x,y，0<size<=40000）"), 400
        w = h = size
        square = True

    with slide_cache.borrow_pair(entry) as pair:
        # context 解析在解码前（§7.4）；失败 → 稳定 4xx/409
        try:
            ctx, fp = _resolve_pair(pair, skey, path=path,
                                    token=render_tok,
                                    flag=_multichannel_enabled(),
                                    revision=_share_revision(desc))
        except (slide_render.RenderRequestError,
                slide_io.SlideRenderError) as e:
            return _render_error(e)
        osr = _region_view(pair, ctx, fp)
        width, height = osr.dimensions
        if square:
            # 旧 size 兼容正方形分支：clamp 到图像边界（独立保留，§6.2）
            x2 = min(x, max(0, width - 1))
            y2 = min(y, max(0, height - 1))
            max_w = max(0, width - x2)
            max_h = max(0, height - y2)
            w2 = h2 = min(w, max_w, max_h)
            if w2 <= 0:
                return jsonify(error="裁剪区域超出图像边界"), 400
        else:
            # v2 矩形：不 clamp——输出尺寸精确等于合法 w/h，出界直接拒绝
            if x + w > width or y + h > height:
                return jsonify(error="裁剪区域超出图像边界（w/h 不做自动裁剪）"), 400
            x2, y2, w2, h2 = x, y, w, h
        # 像素硬闸：真实 w*h，任何解码前拒绝（docs §3.5 / §6.2）
        try:
            crop_guard.check_pixel_limit(w2, h2)
        except crop_guard.CropTooLargeError as e:
            return jsonify(error=str(e), code=e.code,
                           max_pixels=crop_guard.CROP_MAX_PIXELS), 413
        # 每分钟像素预算（按 share capability）+ 并发闸：read_region 前。
        # 真实成本 = 像素量 × 启用通道数（§7.4）
        n_channels = len(ctx.get("active_channels") or ()) if ctx else 1
        allowed, retry_after = crop_guard.admit_pixels(
            token, w2 * h2 * max(1, n_channels))
        if not allowed:
            resp = jsonify(error="crop 请求过于频繁，请稍后重试",
                           code="crop_rate_limited", retry_after=retry_after)
            resp.headers["Retry-After"] = str(int(max(1, retry_after)))
            return resp, 429
        slot = crop_guard.acquire_slot()
        if slot is None:
            return jsonify(error="crop 并发已达上限，请稍后重试",
                           code="crop_busy"), 429
        try:
            region = osr.read_region((x2, y2), 0, (w2, h2)).convert("RGB")
        finally:
            crop_guard.release_slot(slot)

    buf = io.BytesIO()
    region.save(buf, format="PNG")
    buf.seek(0)

    stem = Path(desc.original_filename or safe).stem
    fp_suffix = ("_%s" % str(fp)[:8]) \
        if (_multichannel_enabled() and fp) else ""
    if w2 == h2:
        download_name = "%s_%d_%d_%dpx%s.png" % (stem, x2, y2, w2, fp_suffix)
    else:
        download_name = "%s_x%d_y%d_%dx%dpx%s.png" % (
            stem, x2, y2, w2, h2, fp_suffix)
    return send_file(
        buf,
        mimetype="image/png",
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/s/<token>/api/slide/<name>/thumbnail")
def share_slide_thumbnail(token, name):
    """返回缩略图 JPEG（用作查看器底图预览，慢网下避免瓦片未到区域变白）。

    Batch 3（§6.3）：接受 ``?render=<token>``；缩略图与最低层同 context。
    image-transport-upgrade（§3.2/§4.7/§5.2/§5.4）：与主站同一策略——按实际
    渲染模式显式采样（荧光显式 4:4:4，不再依赖默认色度采样）；additive
    ``?profile=&dv=``；private no-cache + 强 ETag。
    """
    share = _require_share(token)
    desc = _require_slide(share, name)
    return _share_thumbnail_impl(desc)


@app.route("/s/<token>/api/slides/<slide_id>/thumbnail")
def share_slide_thumbnail_by_id(token, slide_id):
    """分享端 ID 原生缩略图（P2，合同 §4）。"""
    share = _require_share(token)
    desc = _require_slide_by_id(share, slide_id)
    return _share_thumbnail_impl(desc)


def _share_thumbnail_impl(desc):
    """分享端缩略图共用实现（P2，合同 §4；P6 收口：revision 按解析后路径）。"""
    safe = desc.legacy_filename
    skey = _slide_scope_key(desc)
    path = slide_storage.resolve_descriptor_path(desc, root=UPLOAD_DIR)
    entry = _get_slide(safe, path)
    render_tok = request.args.get("render") or ""
    try:
        req_profile, req_dv = viewer_display.parse_display_params(
            request.args, "thumbnail")
    except viewer_display.DisplayParamError as e:
        return _display_param_error_response(e)
    with slide_cache.borrow_pair(entry) as pair:
        try:
            ctx, fp = _resolve_pair(pair, skey, path=path,
                                    token=render_tok,
                                    flag=_multichannel_enabled(),
                                    revision=_share_revision(desc))
        except (slide_render.RenderRequestError,
                slide_io.SlideRenderError) as e:
            return _render_error(e)
        image_mode = slide_render.image_mode_from_context(ctx)
        try:
            spec = slide_render.resolve_viewer_encoding(
                image_mode, "thumbnail", req_profile)
        except slide_render.RenderRequestError as e:
            return _render_error(e)
        if req_dv is not None:
            cur_dv = viewer_display.compute_display_version(entry, fp, spec)
            if cur_dv != req_dv:
                return _display_conflict_response(
                    viewer_display.DisplayVersionConflict())
        src = _region_view(pair, ctx, fp)
        thumb = src.get_thumbnail((400, 400))
    if thumb.mode != "RGB":
        thumb = thumb.convert("RGB")
    data, _actual = slide_render.encode_viewer_jpeg(thumb, spec)
    return _tile_jpeg_response(data)


@app.route("/s/<token>/api/roi", methods=["POST"])
def share_roi_add(token):
    share = _require_share(token)
    _log_share_access(token, detail={"via": "api_roi_add"})
    # 权限三档（docs §5.4）：无 annotate 权限的 token 写标注 403。
    # 旧链接无 permissions 字段 → 默认含 annotate（_share_has_annotate 兼容）。
    if not _share_has_annotate(share):
        return jsonify(error="该链接不允许标注"), 403
    body = request.get_json(silent=True) or {}
    slide = body.get("slide")
    slide_id_in = body.get("slide_id")  # P2 前端缺口②：双字段（ID 优先）
    label = body.get("label")
    typ = body.get("type", "rect")

    # label 必填：去空白后非空
    if not isinstance(label, str) or not label.strip():
        return jsonify(error="请填写用户名或标签"), 400

    if typ not in share_store.ROI_TYPES:
        return jsonify(error="未知标注类型"), 400

    if not slide and not slide_id_in:
        return jsonify(error="缺少 slide"), 400
    # 双字段解析（P2 前端缺口② / 合同 §2.1）：slide_id 权威优先，slide 走
    # 冻结别名；两者同给且解析到不同资产 → 400 slide_ref_conflict。
    roi_desc = None
    if slide_id_in:
        try:
            roi_desc = slide_store.resolve_slide_id(str(slide_id_in).strip())
        except Exception:
            app.logger.warning("分享标注写通道 slide_id 解析失败（fail-closed）",
                               exc_info=True)
            roi_desc = None
        if roi_desc is None:
            return jsonify(error="slide 不属于该分享"), 403
    safe = None
    desc_path = None
    if slide:
        safe = _sanitize_name(slide)
        if not safe or safe != slide:
            return jsonify(error="slide 不属于该分享"), 403
        try:
            desc_by_name = slide_store.resolve_legacy_alias(safe)
        except Exception:
            app.logger.warning("分享标注写通道 legacy 解析失败：%s", safe,
                               exc_info=True)
            desc_by_name = None
        if (roi_desc is not None and desc_by_name is not None
                and desc_by_name.slide_id != roi_desc.slide_id):
            return jsonify(error="slide 与 slide_id 指向不同资产",
                           code="slide_ref_conflict"), 400
        if roi_desc is None:
            roi_desc = desc_by_name
    if (roi_desc is None
            or not _slide_member_readable(roi_desc)
            or not _share_slide_member(token, roi_desc.slide_id)):
        return jsonify(error="slide 不属于该分享"), 403
    # P6 运行时退役：legacy 布局成员不可标注（几何校验需读入口文件——读
    # 路径只认 id_bundle）；路径经 resolver 统一解析。
    try:
        desc_path = slide_storage.resolve_descriptor_path(
            roi_desc, root=UPLOAD_DIR)
    except ValueError:
        return jsonify(error="slide 不属于该分享"), 403
    # 2026-10-08 缺陷修复：ID-only 资产无 legacy 名——名快照回退
    # original_filename（与 /api/annotation、/api/share/create 的快照口径
    # 一致），避免 rois.slide / change_log.slide（NOT NULL）落 NULL。
    safe = roi_desc.legacy_filename or roi_desc.original_filename or ""
    # Stage 3c-2（docs §v1.5）：归档项目内切片只读，guest 亦不可标注。
    # P2（缺口②）语义保持：归档判定仍按 legacy 名（ID-only → None 走
    # slide_id 通道——project_slides 对 id_bundle 成员的快照是 ""，用
    # original_filename 名比对会漏判）。
    if _reject_archived_slide(share, roi_desc.legacy_filename,
                              slide_id=roi_desc.slide_id):
        return jsonify(error="切片已归档只读"), 403

    # 收集几何字段透传给 store 校验。升级 C：rect 增加成对 w/h（v2）。
    geom = {}
    for k in ("x", "y", "w", "h", "side_px", "size_mm", "x1", "y1", "x2", "y2",
              "points"):
        if k in body:
            geom[k] = body[k]

    # rect（含未指定默认 rect）需通过分享矩形策略 + 真实几何 + 可信 MPP 校验
    # （§6.4：服务端不能只查客户端自报 size_mm=6；roi_sizes 只作用于
    # preset_only 档，custom 档允许任意宽高但仍受几何/边界校验）。
    if typ == "rect":
        if _rect_policy_of(share) != "custom":
            reject = _reject_preset_rect(share, safe, geom, path=desc_path)
            if reject is not None:
                return jsonify(error=reject[0]), reject[1]
        # 真实切片边界校验（不静默裁剪；store 纯数值校验之外的第二道）
        try:
            eff_w = int(geom.get("w") if geom.get("w") is not None
                        else geom.get("side_px"))
            eff_h = int(geom.get("h") if geom.get("h") is not None
                        else geom.get("side_px"))
            eff_x = int(geom.get("x"))
            eff_y = int(geom.get("y"))
        except (TypeError, ValueError):
            eff_w = eff_h = None
        if eff_w is not None:
            msg = _reject_rect_out_of_bounds(safe, eff_x, eff_y, eff_w, eff_h,
                                             path=desc_path)
            if msg:
                return jsonify(error=msg), 400

    # note 可选（备注文本），透传给 store 校验/清洗
    note = body.get("note", "")

    # §6.3 几何锁可选（草稿保存时带锁）；非布尔 → 400
    geometry_locked = body.get("geometry_locked")
    if geometry_locked is not None and not isinstance(geometry_locked, bool):
        return jsonify(error="geometry_locked 需为布尔值"), 400

    try:
        # P3-1：分享端标注是人工写路径——显式 source="human"（本就非
        # ADMIN_TOKEN、推断即 human，这里显式化以防 token 判定规则漂移；
        # review_status 保持 none，不出「接受」待审徽标）。
        roi = share_store.add_roi(
            token, safe, label, type=typ, note=note,
            source="human",
            visitor=_visitor_stored(_visitor_id()),
            slide_id=roi_desc.slide_id,
            geometry_locked=bool(geometry_locked) if geometry_locked else False,
            **geom
        )
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, index=roi["index"])


def _request_annotation_id(body):
    """旧 index 路由可选携带的 annotation_id（body 优先，query 兜底）。"""
    aid = (body or {}).get("annotation_id")
    if isinstance(aid, str) and aid:
        return aid
    aid = request.args.get("annotation_id")
    if isinstance(aid, str) and aid:
        return aid
    return None


def _guard_roi_index_annotation_id(r, expected_aid):
    """旧 index 路由的 annotation_id 一致性防护（与 app.py 同口径）。

    index 在列表变动后会指向另一条标注：带了 annotation_id 的请求与当前
    指向行不一致 → 409 annotation_moved，绝不删/改错。在调用方完成归属
    校验（403 优先）之后调用，不向未授权访客泄露该 index 处的 id。
    """
    if not expected_aid:
        return None
    if r is not None and (r.get("annotation_id") or "") != expected_aid:
        return jsonify(error="annotation_moved",
                       annotation_id=r.get("annotation_id")), 409
    return None


def _share_roi_by_id_in_token(token, annotation_id):
    """按 annotation_id 取**本 token**下的 live 标注（id 版 get_roi(token,index)）。

    与 index 版同一限定：token 不符（含他人/管理员的标注）或已删 → 同一
    404「选区不存在」。返回 roi dict 或 (resp, status)。
    """
    if not isinstance(annotation_id, str) or not annotation_id:
        return jsonify(error="选区不存在"), 404
    r = share_store.get_roi_by_annotation_id(annotation_id)
    if r is None or r.get("deleted") or (r.get("token") or "") != token:
        return jsonify(error="选区不存在"), 404
    return r


@app.route("/s/<token>/api/roi/<int:index>", methods=["PATCH"])
def share_roi_update(token, index):
    """编辑本 token 的标注几何、备注或几何锁（§6.3）。

    JSON body: {"geom": {...}, "note": "...", "geometry_locked": bool}
    （均可缺省，但至少其一）。
    可选 {"expected_revision": int}（Stage 3c-1 CAS），不符 → 409 revision_conflict。
    §6.3 几何锁：geometry_locked 与 geom 同请求 → 400 lock_with_geometry；
    锁切换必须带 expected_revision（400 expected_revision_required）；已锁标注
    改几何 → 409 annotation_locked（行锁内判定，优先于 revision_conflict）。
    设备归属校验：只有创建该标注的访客可编辑（_roi_owned_by）；旧数据
    （无 visitor）按链接级共享允许编辑。越权返回 403（只读访客不能锁/解锁）。
    调 update_roi / set_roi_geometry_locked；update 返回 False 时 404；成功
    返回更新后的 roi dict（含 index）。
    编辑允许自由调大小，不做 6/6.5mm 限制。
    权限三档（docs §5.4）：无 annotate 权限的 token 写标注 403（旧链接默认含 annotate）。
    兼容防护：可带 annotation_id（body/query），与该 index 当前指向行不一致
    → 409 annotation_moved（绝不改错另一条）。客户端已改用 by-id 路由。

    2026-10-17 P1 竞态修复（鉴权/ID 校验/写入绑定同一条记录）：index 只在
    此解析**一次**行（get_roi），归属/防护都绑定该行；写入按该行
    annotation_id 走 update_roi_by_annotation_id / set_roi_geometry_locked
    （by-id 行锁），绝不按 index 二次定位——解析与写入之间列表再漂移也不
    换行；写入时该行已被并发删除 → 404。无论请求是否携带 annotation_id
    均如此。
    """
    share = _require_share(token)
    _log_share_access(token, detail={"via": "api_roi_update"})
    if not _share_has_annotate(share):
        return jsonify(error="该链接不允许标注"), 403
    body = request.get_json(silent=True) or {}
    geom = body.get("geom")
    note = body.get("note")
    if geom is None and note is None and "geometry_locked" not in body:
        return jsonify(error="缺少 geom 或 note"), 400
    r = share_store.get_roi(token, index)
    if r is None:
        return jsonify(error="选区不存在"), 404
    moved = _guard_roi_index_annotation_id(r, _request_annotation_id(body))
    if moved is not None:
        return moved
    return _share_roi_patch(share, token, r, body)


@app.route("/s/<token>/api/roi/id/<annotation_id>", methods=["PATCH"])
def share_roi_update_by_id(token, annotation_id):
    """按稳定 annotation_id 编辑本 token 的标注（share_roi_update 的 id 版）。

    寻址键不随列表增删漂移（过期 index 缺陷根治路径）。鉴权/校验/语义与
    index 版逐项同口径：annotate 档 × 设备归属 × 归档只读 × CAS × 几何锁；
    id 不属于本 token 或已删 → 404「选区不存在」（不泄露）。
    """
    share = _require_share(token)
    _log_share_access(token, detail={"via": "api_roi_update_by_id"})
    if not _share_has_annotate(share):
        return jsonify(error="该链接不允许标注"), 403
    body = request.get_json(silent=True) or {}
    geom = body.get("geom")
    note = body.get("note")
    if geom is None and note is None and "geometry_locked" not in body:
        return jsonify(error="缺少 geom 或 note"), 400
    r = _share_roi_by_id_in_token(token, annotation_id)
    if not isinstance(r, dict):
        return r
    return _share_roi_patch(share, token, r, body)


def _share_roi_patch(share, token, r, body):
    """share_roi_update(id) 共用主体：归属/归档校验后的 PATCH 语义。

    r 已解析（index 版来自 get_roi(token,index)，id 版来自
    _share_roi_by_id_in_token）。geom/note 一律走
    update_roi_by_annotation_id（按 r 的 annotation_id 行锁——2026-10-17 P1
    竞态修复：index 版此前在 fetch 与更新之间按位置 index 二次定位，列表
    漂移会改到别的行；现绑定已解析行，绝不换行）。r 无 annotation_id 的
    历史/异常行 fail-closed → 404（不回退按 index 定位）。返回 Flask 响应。
    """
    geom = body.get("geom")
    note = body.get("note")
    expected = body.get("expected_revision")
    if not _roi_owned_by(r, _visitor_id()):
        return jsonify(error="只能编辑自己创建的标记"), 403
    if _reject_archived_slide(share, r.get("slide")):
        return jsonify(error="切片已归档只读"), 403
    # §6.3 锁字段解析（权限/归属校验之后，不向越权者泄露锁语义）
    lock_value = None
    if "geometry_locked" in body:
        lock_value = body.get("geometry_locked")
        if not isinstance(lock_value, bool):
            return jsonify(error="geometry_locked 需为布尔值"), 400
        if geom is not None:
            return jsonify(error="lock_with_geometry"), 400
        if expected is None:
            return jsonify(error="expected_revision_required"), 400
    # 升级 C（§6.4）：rect 几何编辑在服务端复核生效几何——
    #   - preset_only 分享：编辑后仍须是 roi_sizes 预设正方形（真实几何 + 可信
    #     MPP 反算，两条路径一致，不允许编辑绕过）；
    #   - 真实切片边界校验（不静默裁剪）；
    #   - 旧客户端只给 side_px 编辑 v2 非正方形 → 400（需升级后以 w/h 编辑）。
    if geom is not None and (r.get("type") or "rect") == "rect":
        safe_name = r.get("slide") or ""
        try:
            eff = share_shared._effective_rect_geometry(r, geom)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if _rect_policy_of(share) != "custom":
            reject = _reject_preset_rect_mm(share, safe_name, eff["w"], eff["h"])
            if reject is not None:
                return jsonify(error=reject[0]), reject[1]
        msg = _reject_rect_out_of_bounds(
            safe_name, eff["x"], eff["y"], eff["w"], eff["h"])
        if msg:
            return jsonify(error=msg), 400
    # §6.3 锁切换（行锁事务内 CAS；操作者只记主体种类，不落访客哈希）
    chained_expected = expected
    lock_result = None
    aid = r.get("annotation_id")
    if not aid:
        # 无可绑定的稳定 id → fail-closed（PG 建行即生成 aid，此为防御分支）
        return jsonify(error="选区不存在"), 404
    if lock_value is not None:
        try:
            lock_result = share_store.set_roi_geometry_locked(
                aid, lock_value, expected_revision=expected, actor="visitor")
        except share_store.RevisionConflict as e:
            return jsonify(error="revision_conflict",
                           current_revision=e.current_revision), 409
        if lock_result is False:
            return jsonify(error="选区不存在"), 404
        chained_expected = lock_result.get("revision")
    if geom is not None or note is not None:
        try:
            updated = share_store.update_roi_by_annotation_id(
                aid, geom=geom, note=note,
                expected_revision=chained_expected)
        except share_store.AnnotationLocked as e:
            return jsonify(
                error="annotation_locked",
                current=annotation_access.public_roi_view(
                    e.current, access_token=token)), 409
        except share_store.RevisionConflict as e:
            return jsonify(error="revision_conflict",
                           current_revision=e.current_revision), 409
        except ValueError as e:
            return jsonify(error=str(e)), 400
        if updated is False:
            return jsonify(error="选区不存在"), 404
        return jsonify(_public_roi(updated, access_token=token))
    return jsonify(_public_roi(lock_result, access_token=token))


def _visitor_read_subject(token):
    """当前请求在 token 上的读取主体（visitor subject；0056 工单 A）。"""
    return annotation_access.visitor_subject(
        token, _visitor_stored(_visitor_id()))


@app.route("/s/<token>/api/rois")
def share_roi_list(token):
    """返回本 token 可见的全部标注（仅授权集合——工单 A / P0 数据隔离）。

    可见性统一经 annotation_access（visitor subject）判定，先过滤再组装：
      - 本 token 上本人创建的记录（visitor 哈希匹配；旧「无 visitor 字段」
        链接级记录兼容）→ source=me（可编辑）；
      - 本 token 上 shared=true 的他人记录 → 本链接只读（不再外溢到同片
        兄弟链接）；
      - annotation_grants 授予本 token 的标注（管理员策展显式授权，
        list_shared_rois_for_slides 现在必须携带 share_token）→
        source=admin（token=admin）/ shared（其他 token）。
    他人**未公开**的私有标注对本设备不可见（不在 me 也不在 shared）；
    每项 index 沿用 get_roi/delete URL 的 token 内非 tombstone 位置（pre-
    filter，不因过滤重编号）。响应不含 visitor 原始/哈希（_public_roi）。
    """
    share = _require_share(token)
    share_slides = share.get("slides", [])
    subject = _visitor_read_subject(token)
    ctx = annotation_access.access_context_for(subject)
    seen_aids = set()
    out = []

    # 1) 本 token 上主体可见的记录（本人 + 本链接 shared + 旧链接级兼容）
    mine = share_store.list_rois(token, subject=subject, access_context=ctx)
    for r in mine:
        aid = r.get("annotation_id")
        if aid:
            if aid in seen_aids:
                continue
            seen_aids.add(aid)
        rr = _public_roi(r, access_token=token)
        rr["source"] = "me" if annotation_access.can_write_annotation(
            subject, r, ctx) else "shared"
        out.append(rr)

    # 2) 显式授予本 token 的跨 token 标注（策展授权；不再汇入「同片全部
    #    shared」——兄弟链接的公开标注不会出现在这里）
    granted = share_store.list_shared_rois_for_slides(share_slides,
                                                      share_token=token)
    for r in granted:
        aid = r.get("annotation_id")
        if aid:
            if aid in seen_aids:
                continue
            seen_aids.add(aid)
        src_token = r.get("token")
        rr = _public_roi(r, access_token=token)
        rr["source"] = "admin" if src_token == share_store.ADMIN_TOKEN \
            else "shared"
        out.append(rr)

    # 按时间倒序
    out.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return jsonify(out)


@app.route("/s/<token>/api/roi/<int:index>", methods=["DELETE"])
def share_roi_delete(token, index):
    """删除本 token 的标注；管理员标注不可由分享端删除。

    设备归属校验：只有创建该标注的访客可删除（_roi_owned_by）；旧数据
    （无 visitor）按链接级共享允许删除。越权返回 403。
    可选 body {"expected_revision": int}（Stage 3c-1 CAS），不符 → 409。
    §6.3：geometry_locked=true → 409 annotation_locked（先解锁再删）。
    权限三档（docs §5.4）：无 annotate 权限的 token 写标注 403（旧链接默认含 annotate）。
    兼容防护：可带 annotation_id（body/query），与该 index 当前指向行不一致
    → 409 annotation_moved（绝不删错另一条）。客户端已改用 by-id 路由。

    2026-10-17 P1 竞态修复（同 share_roi_update）：index 只解析一次行，删除
    按该行 annotation_id 走 delete_roi_by_annotation_id，绝不按 index 二次
    定位；写入时该行已被并发删除 → 404。无论请求是否携带 annotation_id 均
    如此。
    """
    share = _require_share(token)
    _log_share_access(token, detail={"via": "api_roi_delete"})
    if not _share_has_annotate(share):
        return jsonify(error="该链接不允许标注"), 403
    body = request.get_json(silent=True) or {}
    r = share_store.get_roi(token, index)
    if r is None:
        return jsonify(error="选区不存在"), 404
    moved = _guard_roi_index_annotation_id(r, _request_annotation_id(body))
    if moved is not None:
        return moved
    return _share_roi_delete(share, token, r, body)


@app.route("/s/<token>/api/roi/id/<annotation_id>", methods=["DELETE"])
def share_roi_delete_by_id(token, annotation_id):
    """按稳定 annotation_id 删除本 token 的标注（share_roi_delete 的 id 版）。

    寻址键不随列表增删漂移（过期 index 缺陷根治路径）。鉴权/校验/语义与
    index 版逐项同口径：annotate 档 × 设备归属 × 归档只读 × CAS × 几何锁；
    id 不属于本 token 或已删 → 404「选区不存在」（不泄露）。
    """
    share = _require_share(token)
    _log_share_access(token, detail={"via": "api_roi_delete_by_id"})
    if not _share_has_annotate(share):
        return jsonify(error="该链接不允许标注"), 403
    body = request.get_json(silent=True) or {}
    r = _share_roi_by_id_in_token(token, annotation_id)
    if not isinstance(r, dict):
        return r
    return _share_roi_delete(share, token, r, body)


def _share_roi_delete(share, token, r, body):
    """share_roi_delete(id) 共用主体：归属/归档校验后的 tombstone 删除。

    一律按 r 的 annotation_id 行锁删除（delete_roi_by_annotation_id）——
    2026-10-17 P1 竞态修复：index 版此前在 fetch 与删除之间按位置 index
    二次定位（delete_roi(token, r["index"])），列表漂移会删到别的行；现
    绑定已解析行，绝不换行。r 无 annotation_id 的历史/异常行 fail-closed
    → 404（不回退按 index 定位）。
    """
    expected = body.get("expected_revision")
    if not _roi_owned_by(r, _visitor_id()):
        return jsonify(error="只能编辑自己创建的标记"), 403
    if _reject_archived_slide(share, r.get("slide")):
        return jsonify(error="切片已归档只读"), 403
    aid = r.get("annotation_id")
    if not aid:
        return jsonify(error="选区不存在"), 404
    try:
        ok = share_store.delete_roi_by_annotation_id(
            aid, expected_revision=expected)
    except share_store.AnnotationLocked as e:
        return jsonify(
            error="annotation_locked",
            current=annotation_access.public_roi_view(
                e.current, access_token=token)), 409
    except share_store.RevisionConflict as e:
        return jsonify(error="revision_conflict",
                       current_revision=e.current_revision), 409
    if not ok:
        return jsonify(error="选区不存在"), 404
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# 评论线程（guest）—— Stage 3c-1（docs §5.3）
#
# guest 经 /s/* 评论，按 annotation_id 定位。校验（0056 工单 A / P0）：标注
# 所在 slide ∈ 本次分享 × **标注本身对该访客可见**（annotation_access 按
# visitor subject 判定——他人未公开的私有标注不可评论/不可读，403/404 不泄
# 露正文）。评论需 share 含 annotate 权限。author_user_id 留空（guest），
# author_label 取 body.name 或"访客"。
# --------------------------------------------------------------------------- #
def _resolve_anno_in_share(share, annotation_id):
    """按 annotation_id 取标注并校验 slide ∈ share 且标注对本访客可见。

    返回 (roi, None) 或 (None, error_resp)；不可见/不存在统一 404 语义
    （slide 不在分享内仍 403——token 本身可见性不变）。
    P2 收口（合同 §3.1/R-04）：slide ∈ share 判定改 share_slides ID 关系
    （roi.slide_id）；无 ID 的历史行（unresolved）按 JSONB 名快照兜底。
    """
    roi = share_store.get_roi_by_annotation_id(annotation_id)
    if roi is None:
        return None, (jsonify(error="标注不存在"), 404)
    slide = roi.get("slide")
    roi_sid = roi.get("slide_id")
    if roi_sid:
        if not _share_slide_member(share.get("token") or "", roi_sid):
            return None, (jsonify(error="无权访问"), 403)
    elif not slide or slide not in share.get("slides", []):
        return None, (jsonify(error="无权访问"), 403)
    subject = _visitor_read_subject(share.get("token") or "")
    if not annotation_access.can_read_annotation(subject, roi):
        return None, (jsonify(error="标注不存在"), 404)
    return roi, None


@app.route("/s/<token>/api/comments")
def share_comments_list(token):
    """列出某标注的评论（guest 视角，query: annotation_id）。share 有效即可查看。

    展示 J / 公开分享页红线：登录用户（author_user_id 非空）的作者身份一律
    **掩码**展示（email/login_id 经 mask_login_id），绝不外泄完整邮箱，也
    **不得**回退 display_name（历史行里的 author_label 快照同样替换）。
    guest 评论（author_user_id 为空）保留访客自报 label。
    """
    share = _require_share(token)
    annotation_id = request.args.get("annotation_id")
    if not annotation_id:
        return jsonify(error="缺少 annotation_id"), 400
    _roi, err = _resolve_anno_in_share(share, annotation_id)
    if err:
        return err
    comments = share_store.list_comments(
        annotation_id=annotation_id, project=False)
    # 掩码投影：按 author_user_id 批量解析身份再掩码（查询失败按 None 处理，
    # 降级为通用「成员」，绝不回退明文 label/display_name）
    out = []
    for c in comments:
        c = dict(c)
        author_uid = c.get("author_user_id")
        if author_uid:
            masked = None
            try:
                u = user_store.get_user(author_uid) or {}
                identity = (u.get("email_normalized") or u.get("email")
                            or u.get("login_id") or "")
                if identity:
                    masked = registration_store.mask_login_id(identity)
            except Exception:
                masked = None
            c["author_label"] = masked or "成员"
            c.pop("author_email", None)
        out.append(annotation_access.public_comment_view(c, access_token=token))
    return jsonify({"comments": out})


@app.route("/s/<token>/api/comments", methods=["POST"])
def share_comment_add(token):
    """在某标注下新增评论（guest）。需 share 含 annotate 权限。

    JSON: {annotation_id, body, parent_id?, name?}。
    """
    share = _require_share(token)
    _log_share_access(token, detail={"via": "api_comment_add"})
    if not _share_has_annotate(share):
        return jsonify(error="该链接不允许评论"), 403
    body = request.get_json(silent=True) or {}
    annotation_id = body.get("annotation_id")
    text = body.get("body")
    parent_id = body.get("parent_id")
    label = (body.get("name") or "").strip() or None
    if not annotation_id:
        return jsonify(error="缺少 annotation_id"), 400
    roi, err = _resolve_anno_in_share(share, annotation_id)
    if err:
        return err
    # Stage 3c-2（docs §v1.5）：归档项目内切片只读，guest 亦不可评论
    if _reject_archived_slide(share, roi.get("slide")):
        return jsonify(error="切片已归档只读"), 403
    try:
        cmt = share_store.add_comment(
            annotation_id, roi.get("slide"), token, text,
            author_user_id=None, author_label=label, parent_id=parent_id,
            slide_id=roi.get("slide_id"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, comment=cmt)


@app.route("/static/<path:filename>")
def share_static(filename):
    return send_from_directory("static", filename)


# --------------------------------------------------------------------------- #
# 启动只读 probe（review G1）：存储损坏/不可读 → worker 不 ready
# --------------------------------------------------------------------------- #
# gunicorn 以 share_server:combined_app 拉起 worker，模块加载即 probe：
# 失败直接抛错使 worker boot 失败（服务不 ready），绝不在不可判读状态下
# 对外服务空库。失败信息只含错误类别与安全路径标识，不含数据内容。
SHARE_STORE_STARTUP_ERROR = None


def run_share_store_startup_probe():
    """执行启动 probe；失败记录到 SHARE_STORE_STARTUP_ERROR 并抛 SystemExit。

    独立成函数便于部署脚本/测试显式调用。
    """
    global SHARE_STORE_STARTUP_ERROR
    try:
        share_store.probe_readable()
    except (share_store.ShareStoreCorrupt, share_store.ShareStoreUnavailable) as e:
        SHARE_STORE_STARTUP_ERROR = "%s: %s" % (type(e).__name__, e)
        print("[share_server] FATAL share 存储启动 probe 失败：%s"
              % SHARE_STORE_STARTUP_ERROR, file=sys.stderr, flush=True)
        raise SystemExit(2)
    return True


run_share_store_startup_probe()


# --------------------------------------------------------------------------- #
# 合并 WSGI 应用（模块级，供 gunicorn 直接引用 share_server:combined_app）
# --------------------------------------------------------------------------- #
# 同一端口按路径分流：
# /s/...（含 /s/<token>/ 全部分享路由）→ 分享应用；
# 其余（/、/login、/api/*、/static/*）→ 管理端应用（开启认证
# （REQUIRE_ADMIN_AUTH / 存在用户）后需登录），实现"同入口不同页面"：
# 访问 / 是管理员登录
# 门户，进 /s/<token> 是正常分享页。这样 frp 不需要为管理端另开隧道，
# 复用既有分享隧道即可。
# import app 会触发 app.py 模块级代码，可接受——两者本就要在 gunicorn
# 线程 worker 下共存（app 也作为 WSGI 对象被 gunicorn 引用）。
import app as admin_app


def combined_app(environ, start_response):
    path = environ.get("PATH_INFO") or ""
    if path == "/s" or path.startswith("/s/"):
        return app(environ, start_response)  # 分享应用
    return admin_app.app(environ, start_response)  # 管理端应用


if __name__ == "__main__":
    # 本地开发 fallback（生产由 Containerfile 直接调 gunicorn，不走 __main__）：
    # HTTPS：提供 SHARE_TLS_CERT / SHARE_TLS_KEY 时直接以 TLS 运行
    # （frp TCP 隧道只是转发，TLS 需在本服务终止，避免被备案系统按 HTTP 拦截）
    tls_cert = os.environ.get("SHARE_TLS_CERT")
    tls_key = os.environ.get("SHARE_TLS_KEY")
    ssl_context = None
    if tls_cert and tls_key and os.path.exists(tls_cert) and os.path.exists(tls_key):
        ssl_context = (tls_cert, tls_key)
        print(f"[share_server] HTTPS enabled: {tls_cert}")
    else:
        print("[share_server] WARNING: 未找到 TLS 证书，以 HTTP 运行")

    from werkzeug.serving import run_simple

    run_simple(
        "0.0.0.0",
        int(os.environ.get("SHARE_PORT", 38000)),
        combined_app,
        threaded=True,
        ssl_context=ssl_context,
    )
