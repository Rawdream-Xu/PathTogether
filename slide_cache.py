# -*- coding: utf-8 -*-
"""切片句柄池与元数据缓存（app.py 与 share_server.py 共享）。

早期实现用 per-slide 互斥锁保护单个 OpenSlide 句柄：``with entry["lock"]``。
问题在于 openslide.read_region 非线程安全，加锁后同一切片的并发 tile 请求全部
串行——浏览器一次拉 8 个瓦片会排队，慢网下画面割裂明显。

这里改为「句柄池」：
- 每个切片持有 N 个 (osr, dz) 句柄，``borrow_pair`` 取出一个用完归还；
- 池空且并发超过 N 时 ``sem.acquire()`` 阻塞等待归还——至少 N 路真并行，
  优于全锁串行；
- 句柄池大小由环境变量 SLIDE_HANDLE_POOL 控制（默认 6），同一进程内每个
  切片独立一个池。openslide 句柄非 fork 安全，生产用线程 worker（不 preload、
  不用 gevent/eventlet），每 worker 进程各自持有一份独立的池与缓存。

文件换代（review G3 / CACHE-1）
-------------------------------
管理端删除/重传同名切片后，分享进程里的旧 OpenSlide 句柄仍指向旧 inode：
- 只比 mtime 的 info 缓存会用旧句柄读出新 mtime 再回填（错配）；
- JPEG 瓦片缓存跨代命中（旧图当新图服务）；
- 分享进程从不调用 evict，旧句柄永不释放。

现在 handle / info / tile 三层由**同一套**文件身份驱动：

- ``FileSignature``：``(st_dev, st_ino, st_size, st_mtime_ns)``。dev+ino 保证
  同名 ``os.replace``（换 inode）立即换代；size+mtime_ns 兜底原地改写。
  仅比 mtime 不够——保持 mtime 的替换（备份恢复/_touch 回写）会漏判。
- generation：每个缓存 entry 一个单调递增整数。签名变化 → generation+1，
  旧代句柄 retire。
- retire/return 状态转换：换代时（或 evict 时）池内**空闲**旧代句柄立即
  关闭；**在途**（已借出）旧代句柄在归还时关闭（``pair.gen !=
  entry.generation`` 即 retired）。在途请求可继续用旧句柄完成（读到的
  是一致性的旧内容），但结果不得按新代缓存。
- 元数据缓存按 FileSignature 键控（不再只比 mtime）；tile JPEG 缓存由
  share_server 按 (name, generation, level, x, y) 键控。
- 读取前后签名变化 → 丢弃结果最多重试一次（``read_stable`` /
  ``cached_read_metadata``）；仍变 → ``SlideFileChanged``（fail-closed，
  不缓存旧代内容）。
- 主站 ``evict`` 仅是快速路径：正确性不依赖它——跨进程（share_server 与
  app 各自进程）由借用时的签名检查兜底，未 evict 也会在下次借用换代。

普通图片（BMP/JPEG，raster-image-compatibility §4.6）
-----------------------------------------------------
- **单解码句柄**：普通图片不是可按需读块的金字塔切片，整幅 RGB 缓冲常驻
  （50MP ≈ 150MB/张）。按路径（``slide_io.is_raster_ext``）识别普通图片，
  信号量/池上限取 **1**——并发请求同图在信号量上自然串行排队；借还锁与
  换代（FileSignature/generation/retire）语义完全不变。
- **有界缓存/惰性 LRU**：``_slide_cache`` 原本只增不减（仅 evict 快路径），
  对全量解码的普通图片不能只记录风险就声称内存有界。普通图片条目额外记入
  ``_raster_lru``（容量 ``RASTER_CACHE_MAX_ENTRIES``，env 可配，默认 4）；
  ``get_slide`` 惰性触发淘汰（不新增常驻线程）：超出预算时最久未用条目按
  现有换代纪律退休（空闲句柄立即关、在途句柄归还即关——活跃条目不会被
  强关），并清其 info 缓存。非普通图片条目行为保持不变。
"""

import contextlib
import os
import queue
import threading
from collections import OrderedDict
from typing import Callable, NamedTuple, Optional, Tuple

import openslide
from openslide.deepzoom import DeepZoomGenerator

import slide_io

# Deep Zoom 参数（与主应用保持一致）
DZ_TILE_SIZE = 512
DZ_OVERLAP = 1

# 句柄池大小：每个切片可并行的句柄数（默认 6）
SLIDE_HANDLE_POOL = int(os.environ.get("SLIDE_HANDLE_POOL") or 6)

# 普通图片缓存预算（§4.6）：RGB 常驻 ≈ W×H×3 字节（50MP ≈ 150MB/张；Wave 1
# 实测 48MP BMP 冷打开峰值 RSS ~217MB，含解码/转置瞬时开销）。默认按目标
# 进程内存 1–2GB 取 **4 张**起步：4 × 150MB ≈ 600MB 常驻上限，叠加一次解码
# 的 ~220MB 瞬时峰值与瓦片 JPEG 编码开销仍在预算内，同时给「多图对比查看」
# 留出实用的工作集。环境变量 RASTER_CACHE_MAX_ENTRIES 可覆盖（非法/非正值
# 回退默认；模块加载期读取，测试可 monkeypatch 本常量）。
_DEFAULT_RASTER_CACHE_MAX = 4


def _env_raster_cache_max():
    """读取 ``RASTER_CACHE_MAX_ENTRIES`` 环境变量（非法/缺省回退默认值）。"""
    raw = os.environ.get("RASTER_CACHE_MAX_ENTRIES")
    if not raw:
        return _DEFAULT_RASTER_CACHE_MAX
    try:
        v = int(raw)
    except ValueError:
        return _DEFAULT_RASTER_CACHE_MAX
    return v if v > 0 else _DEFAULT_RASTER_CACHE_MAX


RASTER_CACHE_MAX_ENTRIES = _env_raster_cache_max()

# 借用期间文件被连续替换的最大重试次数（正常 1 次内必成；只是病态竞争兜底）
_MAX_ACQUIRE_ATTEMPTS = 4


class FileSignature(NamedTuple):
    """切片文件身份：同名替换（换 inode）/原地改写（size/mtime）都能判别。"""

    st_dev: int
    st_ino: int
    st_size: int
    st_mtime_ns: int


def signature_of(path) -> Optional[FileSignature]:
    """取文件签名；stat 失败（删除/权限）返回 None（调用方按不可判读处理）。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return FileSignature(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


class SlideFileChanged(Exception):
    """读取期间文件签名持续变化（超过一次重试），或无法取得稳定代句柄。

    fail-closed：调用方应丢弃结果（不缓存、不按当前代输出），向用户返回
    可重试错误（share_server 映射 503 slide_file_changed）。
    """


# 切片缓存：name -> {"name", "path", "pool", "sem", "raster", "created_handles",
#                    "generation", "signature", "retired", "gen_lock"}
_slide_cache: dict = {}
_cache_lock = threading.Lock()

# 普通图片 LRU（仅收录 entry["raster"] 条目；name -> None，尾部 = 最近使用）。
# 读写均在 _cache_lock 内；淘汰对象按尾部→头部（最久未用优先）。
_raster_lru: "OrderedDict[str, None]" = OrderedDict()

# 元数据缓存：name -> (FileSignature, meta_dict)（签名感知，文件未变则复用）
_info_cache: dict = {}
_info_cache_lock = threading.Lock()


def _make_pair(path, generation):
    """打开一个属于指定 generation 的 (osr, dz) 句柄对。"""
    osr = slide_io.open_slide(path)
    dz = DeepZoomGenerator(
        osr, tile_size=DZ_TILE_SIZE, overlap=DZ_OVERLAP, limit_bounds=True
    )
    return {"osr": osr, "dz": dz, "gen": generation}


def _new_entry(name, path):
    """创建空句柄池 entry（初始不含任何打开句柄，首次借用时惰性创建）。

    generation 从 0 起；signature 惰性（首次借用时 stat）。retired 一旦
    True 表示该 entry 至少换过一代/被 evict 过（在途旧代句柄归还即关）。

    普通图片（按路径后缀 ``slide_io.is_raster_ext`` 识别）：``raster=True``，
    信号量/池上限取 1（§4.6 单解码句柄）；其余格式保持 SLIDE_HANDLE_POOL。
    """
    is_raster = slide_io.is_raster_ext(name)
    return {
        "name": name,
        "path": path,
        "pool": queue.Queue(),
        "raster": is_raster,
        "sem": threading.Semaphore(1 if is_raster else SLIDE_HANDLE_POOL),
        "created_handles": 0,
        "generation": 0,
        "signature": None,
        "retired": False,
        "gen_lock": threading.Lock(),
    }


def _touch_raster_locked(name):
    """LRU 记账：普通图片条目移到「最近使用」尾部（须持 _cache_lock）。"""
    if name in _raster_lru:
        _raster_lru.move_to_end(name)


def _pop_raster_victims_locked():
    """普通图片条目超预算时按最久未用弹出待淘汰 entry（须持 _cache_lock）。

    只淘汰普通图片条目（_raster_lru 即其成员清单）；被挤出的 entry 同时从
    _slide_cache 移除。返回 victim entry 列表（ retirement 在锁外做，避免
    _cache_lock 与 gen_lock 嵌套）。
    """
    victims = []
    while len(_raster_lru) > RASTER_CACHE_MAX_ENTRIES:
        victim_name, _ = _raster_lru.popitem(last=False)
        victim = _slide_cache.pop(victim_name, None)
        if victim is not None:
            victims.append(victim)
    return victims


def _retire_victim_entry(victim):
    """淘汰收尾（锁外）：按换代纪律退休 + 清 info 缓存。

    空闲句柄立即关闭；**在途（借出中）句柄不被强关**——归还时按代差
    （pair.gen != entry.generation）关闭，借出方手里的读取可一致完成。
    """
    with victim["gen_lock"]:
        _retire_generation_locked(victim, signature_of(victim["path"]))
    with _info_cache_lock:
        _info_cache.pop(victim["name"], None)


def get_slide(name, path):
    """从缓存获取（或创建）切片的句柄池 entry。

    打开是惰性的：首次 borrow_pair 时才真正调用 slide_io.open_slide，因此这里
    无需处理"并发打开同一文件"的句柄泄漏（空 entry 被丢弃也无副作用）。

    普通图片条目：命中即刷新 LRU recency；新建后惰性触发 LRU 淘汰
    （超 ``RASTER_CACHE_MAX_ENTRIES`` 时关掉最久未用条目，见模块 docstring）。
    """
    with _cache_lock:
        entry = _slide_cache.get(name)
        if entry is not None:
            if entry.get("raster"):
                _touch_raster_locked(name)
            return entry
    # 缓存未命中：创建空 entry（不在全局锁内，避免阻塞其他切片）
    entry = _new_entry(name, path)
    with _cache_lock:
        existing = _slide_cache.get(name)
        if existing is not None:
            if existing.get("raster"):
                _touch_raster_locked(name)
            return existing
        _slide_cache[name] = entry
        victims = []
        if entry["raster"]:
            _raster_lru[name] = None
            victims = _pop_raster_victims_locked()
    for victim in victims:
        _retire_victim_entry(victim)
    return entry


def _close_pair(pair):
    """关闭一个句柄对（幂等兜底：close 失败吞掉，句柄交给 OS 回收）。"""
    try:
        pair["osr"].close()
    except Exception:
        pass


def _close_idle_pairs_locked(entry):
    """关闭池内全部**空闲**句柄（须持 entry["gen_lock"]）。

    在途（已借出）句柄不在池内，由归还路径按代差关闭。
    """
    pool = entry["pool"]
    while True:
        try:
            pair = pool.get_nowait()
        except queue.Empty:
            break
        _close_pair(pair)


def _retire_generation_locked(entry, new_signature):
    """换代（须持 entry["gen_lock"]）：旧代 retire，空闲句柄立即关。

    - generation+1 并记录新签名（None 表示文件当前不可判读，同样强制换代，
      避免拿旧句柄配新文件）；
    - entry["retired"]=True：标记该 entry 存在已退休代；
    - 在途旧代句柄在归还时（pair.gen != entry.generation）关闭。
    """
    _close_idle_pairs_locked(entry)
    entry["generation"] = entry.get("generation", 0) + 1
    entry["signature"] = new_signature
    entry["retired"] = True


def refresh_generation(entry) -> int:
    """在同一把代锁内比较签名并按需换代，返回当前 generation。

    handle/info/tile 共用的唯一换代入口（review G3：禁止三层缓存各自比
    mtime）。签名未变时是纯 stat 快路径。
    """
    with entry["gen_lock"]:
        sig = signature_of(entry["path"])
        if sig != entry["signature"]:
            _retire_generation_locked(entry, sig)
        return entry["generation"]


def generation_is_current(entry, generation) -> bool:
    """校验某次读取拿到的 generation 是否仍是当前代（读取后校验用）。

    True 当且仅当 entry 未再换代，且文件当前签名仍与该代记录一致——
    覆盖「读取期间文件被换但没有其他线程触发换代」的窗口。
    """
    with entry["gen_lock"]:
        if entry["generation"] != generation:
            return False
        return signature_of(entry["path"]) == entry["signature"]


def _acquire_pair(entry):
    """取一个属于当前代的句柄对；借用等待期间换代则关闭重试。"""
    for _ in range(_MAX_ACQUIRE_ATTEMPTS):
        gen = refresh_generation(entry)
        try:
            pair = entry["pool"].get_nowait()
        except queue.Empty:
            pair = None
        if pair is not None and pair.get("gen") != gen:
            # 池里残留的旧代句柄（换代时被并发借走又归还的窗口）
            _close_pair(pair)
            pair = None
        if pair is None:
            # 惰性打开（慢操作，不在代锁内）
            pair = _make_pair(entry["path"], gen)
            entry["created_handles"] += 1
        with entry["gen_lock"]:
            if entry["generation"] == gen:
                return pair
        # 打开期间文件又换了代：丢弃刚拿到的旧代句柄，重来
        _close_pair(pair)
    raise SlideFileChanged(
        "借用期间切片文件被连续替换，无法获得稳定代句柄：%s" % entry["name"])


def _release_pair(entry, pair):
    """归还句柄：当前代回池复用；旧代（retired）在途句柄归还即关。"""
    if pair is None:
        return
    with entry["gen_lock"]:
        gen = entry["generation"]
    if pair.get("gen") != gen:
        _close_pair(pair)
    else:
        entry["pool"].put(pair)


@contextlib.contextmanager
def borrow_pair(entry):
    """借出一个属于当前 generation 的 (osr, dz) 句柄对，用完归还到池。

    并发数受 entry["sem"] 限制：池空且并发 > N 时阻塞等待归还（至少 N 路并行）。
    普通图片（entry["raster"]）N=1：并发请求同图在信号量上自然串行排队
    （§4.6 单解码句柄）。借出前在同一把代锁内比较文件签名并按需换代
    （同名 replace/原地改写/保持 mtime 均触发）；旧代空闲句柄立即关，
    旧代在途句柄归还时关。
    """
    entry["sem"].acquire()
    pair = None
    try:
        pair = _acquire_pair(entry)
        try:
            yield pair
        finally:
            _release_pair(entry, pair)
    finally:
        entry["sem"].release()
        # _make_pair 失败时 pair 为 None：只归还信号量（避免死锁），
        # 不把 None 放进池里（否则后续借用会拿到 None 报 TypeError）


def evict(name):
    """移除并关闭缓存中该切片的全部句柄，同时清掉其 info 缓存。

    仅作快速路径（主站删除/重传后主动释放）：正确性不依赖本调用——
    跨进程（share_server 不 evict）由 borrow_pair 的签名检查兜底换代。
    在途句柄通过换代标记在归还时关闭。普通图片条目同时移出 LRU。
    """
    with _cache_lock:
        entry = _slide_cache.pop(name, None)
        _raster_lru.pop(name, None)
    if entry is not None:
        with entry["gen_lock"]:
            _retire_generation_locked(entry, signature_of(entry["path"]))
    with _info_cache_lock:
        _info_cache.pop(name, None)


def evict_meta_by_slide(slide_id):
    """按 slide_id 逐出全部元数据缓存行（P0-W6c）。

    缓存键是 ``"<slide_id>::<展示名>"``（app._slide_cache_key_for），值以
    FileSignature（mtime/inode）判定新鲜度——**手补 mpp（slide_mpp_
    overrides）不改变文件签名**，不逐出的话 info 端点会一直回旧 mpp。
    补录/清除补录后由 slide_meta_http 调用；键形态以外（legacy 名键）
    不受影响。展示名可变（R-02），所以按 ``<sid>::`` 前缀整段逐出。
    """
    sid = str(slide_id or "")
    if not sid:
        return
    prefix = sid + "::"
    with _info_cache_lock:
        for key in [k for k in _info_cache if isinstance(k, str)
                    and k.startswith(prefix)]:
            _info_cache.pop(key, None)


def read_stable(entry, fn: Callable[[dict], object]) -> Tuple[object, Optional[int]]:
    """在当前代句柄上执行 fn(pair)，返回 (result, generation)。

    读取前后文件签名变化 → 丢弃结果，换代重读**至多一次**；仍变化 → 抛
    ``SlideFileChanged``（不把旧代内容当当前代返回/缓存）。dzi/tile 等
    「结果要按代缓存」的读取统一走这里（review G3：metadata 与 tile 携带
    同一 generation）。
    """
    for _attempt in (0, 1):
        with borrow_pair(entry) as pair:
            gen = pair.get("gen")
            result = fn(pair)
        if gen is None or generation_is_current(entry, gen):
            return result, gen
    raise SlideFileChanged("读取期间切片文件被连续替换：%s" % entry["name"])


def cached_read_metadata(name, path, read_meta_fn):
    """签名感知的元数据读取：文件身份未变则复用缓存的 meta 部分。

    read_meta_fn() 需返回 meta dict（内部自取句柄）。alias/note 不在缓存内，
    由调用方每次现查并合并。

    与 read_stable 同一套换代纪律：读取前后签名变化 → 丢弃重读至多一次；
    仍变化 → ``SlideFileChanged``。缓存键为 FileSignature（含 inode，同名
    replace 即使 mtime 相同也不命中）。
    """
    sig = signature_of(path)
    if sig is not None:
        with _info_cache_lock:
            hit = _info_cache.get(name)
            if hit is not None and hit[0] == sig:
                return hit[1]
    for _attempt in (0, 1):
        meta = read_meta_fn()
        sig_after = signature_of(path)
        if sig_after is not None and sig_after == sig:
            with _info_cache_lock:
                _info_cache[name] = (sig_after, meta)
            return meta
        # 读取期间文件被替换：以读后签名为准重读一次
        sig = sig_after
    raise SlideFileChanged("读取期间切片文件被连续替换：%s" % name)
