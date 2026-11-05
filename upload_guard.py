# -*- coding: utf-8 -*-
"""上传资源防护（P0-A §3.3，docs/open-registration-security-remediation.md）。

三块职责：

1. **单请求字节上限（计数流）**：``UPLOAD_MAX_REQUEST_BYTES``。不信任
   ``Content-Length``（可缺省 / 可伪造），保存时逐块计数，超限立即停止并
   抛 :class:`RequestTooLarge`。Werkzeug 层的 ``MAX_CONTENT_LENGTH``（app.py
   接线，含少量 multipart 开销余量）负责第一层拦截，本模块的计数流是第二层
   权威——chunked / 伪造 Content-Length 都在上限处停止。
2. **磁盘保留水位**：``UPLOAD_RESERVED_FREE_BYTES``。写临时文件前与 ZIP
   解压过程中检查 ``UPLOAD_DIR`` 所在卷的可用空间，低于水位即拒绝
   （数据库与日志仍有安全余量）。
3. **PG 权威的用户配额 + reservation + 速率**（0013 + 0072 持有者绑定）：
   - **任务持有容量，租约控制执行**（docs/task-capacity-lifecycle-repair
     -agent-plan-20260927.md）：reservation 行可绑定持有者（holder_kind/
     holder_id/purpose，0072）；**已绑定的预约不参加 TTL 过期回收**——
     ``expires_at`` 退化为执行租约时效，容量只在发布结算（consume）或
     **清理确认后**（release）离开账本。任务停止心跳不代表字节消失。
   - ``reserve_upload``：单事务内 ``SELECT ... FOR UPDATE`` 锁配额行 → 惰性
     回收过期**未绑定**预占 → 在途数 / 每小时请求数判定 →
     ``used + reserved + n <= quota`` 条件判定后插入 reservation 并累加
     reserved_bytes。同用户并发预占串行化，不会出现部分写或绕过；
   - ``bind_reservation_locked``：writer 启动前把准备期预约绑定到任务
     （与任务行创建同事务；绑定后不可改归属）；
   - ``topup_reservation``：ZIP 展开后实际总量超过预占时的原子补占；
   - ``release_reservation``（清理确认后释放；绑定预约需持有者上下文）、
     ``consume_reservation``（发布结算转实占：reserved → used）；
   - 每小时请求数上限以 reservation 行的 ``reserved_at`` 为计数源（计尝试
     次数，不论终态；仅 origin='admission'——核账补建不算用户上传）；在途
     上限 = 未结算责任数（已绑定不看租约；未绑定看租约；清理重试中的
     upload_task 责任除外——见 ``reserve_upload_locked`` 注释）。

**后端适用范围（明确声明，不是静默退化）**：
  - 计数流与磁盘水位：纯进程内实现，json/dual/postgres 全部生效；
  - 配额 / 在途 / 每小时限流：仅 ``STORAGE_BACKEND=postgres`` 权威
    （platform_features.require_pg_backend fail-closed）。适用主体是
    ``role=user``（受邀账号，docs §3.3 的威胁模型）；owner 是运维者本人，
    不受限（owner 想自我约束时可直接向 upload_user_quotas 插行并改
    app 层判定）。AUTH_ENABLED=False 的本地免登录形态（user_id 为空）
    同样跳过——本地单机开发语义与现状一致。json 后端 + role=user 的
    上传按仓库既定哲学 fail-closed 返回 503（与 POST /login 在 json 后端
    503 同款），绝不退化进程内计数。

默认值依据（均 env 可调；标 [测] 的需上线前按真实 TCGA/MRXS 分布测
P95/P99 后复核，见模块尾部的 DEFAULTS_RATIONALE）。
"""

import os
import secrets
import shutil
import time

import pg_store
import platform_features

# --------------------------------------------------------------------------- #
# env 可调常量（import 期一次性读取；测试用 monkeypatch 改模块属性）
# --------------------------------------------------------------------------- #
def _env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


#: 单请求字节上限（含 zip 上传本体）。默认 10 GiB：TCGA SVS 单文件常见
#: 0.5–2.5 GiB、MRXS 连伴侣目录可达数 GiB，10 GiB 覆盖极大 specimen 且给
#: 边缘代理留出统一配置空间。[测] 上线前按真实分布 P99 收紧。
#: 注意：边缘代理（frp/nginx）的 body 上限必须与此值一致（docs §3.3-1）。
UPLOAD_MAX_REQUEST_BYTES = _env_int("UPLOAD_MAX_REQUEST_BYTES", 10 * 1024 ** 3)

#: 每用户空间配额字节上限（PG 权威，仅 role=user）。默认 10 GiB：新用户
#: 默认值；早期真实用户由 0091 迁移提到 50 GiB（2026-11-05 产品决定，先不
#: 过度限制）。[测] 按 deploy-host 可用磁盘与单用户合理保有量复核。
#: owner 不受本配额限制；管理员可经 admin API 为单个用户单独调高（硬边界见
#: ``UPLOAD_USER_QUOTA_MIN_BYTES`` / ``UPLOAD_USER_QUOTA_MAX_BYTES``）。
UPLOAD_USER_QUOTA_BYTES = _env_int("UPLOAD_USER_QUOTA_BYTES", 10 * 1024 ** 3)

#: 管理员调整单用户配额时的硬边界（字节）。下限 0 = 立即冻结该用户的新上传
#: （已有文件不删除、used_bytes 不清零）；上限 1 TiB = 平台单用户合理保有量
#: 的硬顶，防止一次误操作使容量管理失去意义。
UPLOAD_USER_QUOTA_MIN_BYTES = 0
UPLOAD_USER_QUOTA_MAX_BYTES = 1024 ** 4

#: 单用户配额调整审计动作（写入与配额变更同一事务）。
UPLOAD_QUOTA_AUDIT_ACTION = "upload.user_quota_update"

#: 磁盘保留水位：UPLOAD_DIR 所在卷 free < 该值时拒绝新写入（507）。
#: 默认 20 GiB：需同时容纳 PG 数据目录、日志与解压暂存的并发余量。
#: [测] 按 deploy-host 磁盘规格与 PG/日志实际增速调整。
UPLOAD_RESERVED_FREE_BYTES = _env_int("UPLOAD_RESERVED_FREE_BYTES", 20 * 1024 ** 3)

#: 每用户在途上传数上限（state='reserved' 未过期）。默认 3：正常用户不会
#: 同时开 3 个以上大文件上传；防单账号并发铺满磁盘。
UPLOAD_MAX_INFLIGHT = max(1, _env_int("UPLOAD_MAX_INFLIGHT", 3))

#: 每用户每小时上传请求数上限（计尝试，不论成败）。默认 60：受邀协作
#: 场景远够用，同时把失败重试风暴压在每小时一个量级。
UPLOAD_HOURLY_REQUEST_LIMIT = max(1, _env_int("UPLOAD_HOURLY_REQUEST_LIMIT", 60))

#: reservation 过期秒数。**未绑定预约**的可回收截止时间（准备期兜底：进程
#: 崩溃后由下一次 reserve 惰性回收）。已绑定预约（0072）不参加 TTL 回收——
#: expires_at 只表示执行租约时效，到期由续租/恢复/清理编排接管，容量
#: 责任不丢。默认 30 分钟：10 GiB 在 5 MB/s 慢链路上约需 35 分钟，取略宽
#: 上界。
UPLOAD_RESERVATION_TTL_SECONDS = _env_int("UPLOAD_RESERVATION_TTL_SECONDS", 1800)

#: 持有者绑定枚举（0072；有限集合，不允许任意字符串）。跨表无外键，
#: 创建/绑定必须同事务，审计双向核验（reconcile_upload_capacity.py）。
HOLDER_KINDS = frozenset({"upload_task", "ingestion_job", "baidu_batch",
                          "producer_import"})

#: 每种持有者的合法用途（通道内固定；需要多份预算的通道显式区分用途，
#: 不新增通用工作流框架）。producer_import（C5 合同 §4）同一 import_id 可持
#: 两份不同用途的绑定预约：final（平台接收产物；发布 consume / 清理确认后
#: release）与 scratch（插件下载+转换+本地输出副本责任；只经 cleanup-confirm
#: 释放）——release/consume 的持有者上下文只比 (kind,id)，两份预约靠任务行的
#: 两个 reservation_id 列区分用途。
HOLDER_PURPOSES = {
    "upload_task": frozenset({"upload"}),          # V1/V2/ZIP 传输+发布
    "ingestion_job": frozenset({"ingest_local"}),  # COS 摄取本地容量
    "baidu_batch": frozenset({"baidu_import"}),    # 百度批次一次性预算
    "producer_import": frozenset({"scratch", "final"}),  # C5 双用途
}

#: Werkzeug MAX_CONTENT_LENGTH = 单请求上限 + multipart 开销余量（表单
#: 边界、字段名等）。真正的文件字节权威仍是计数流。
UPLOAD_MULTIPART_SLACK_BYTES = 1024 ** 2

#: 计数流读取块大小。
CHUNK_SIZE = 1024 * 1024


# --------------------------------------------------------------------------- #
# 业务异常（code 供路由映射稳定错误码）
# --------------------------------------------------------------------------- #
class UploadGuardError(Exception):
    """上传防护业务异常基类。"""

    code = "upload_guard_error"
    http_status = 400


class RequestTooLarge(UploadGuardError):
    """计数流超过单请求字节上限。"""

    code = "upload_too_large"
    http_status = 413


class DiskWatermarkExceeded(UploadGuardError):
    """磁盘可用空间低于保留水位。"""

    code = "disk_watermark_exceeded"
    http_status = 507


class QuotaExceeded(UploadGuardError):
    """用户空间配额不足（used + reserved + n > quota）。"""

    code = "upload_quota_exceeded"
    http_status = 413


class InflightLimitExceeded(UploadGuardError):
    """用户在途上传数达到上限。"""

    code = "upload_inflight_limit"
    http_status = 429


class RateLimitExceeded(UploadGuardError):
    """用户每小时上传请求数达到上限。"""

    code = "upload_rate_limited"
    http_status = 429


class ReservationInvalid(UploadGuardError):
    """reservation 不存在 / 已结算 / 已过期。"""

    code = "upload_reservation_invalid"
    http_status = 500


class ReservationHolderMismatch(ReservationInvalid):
    """绑定预约的持有者校验失败（缺上下文 / 不匹配 / 重复绑异主）。

    生命周期模型（0072）：绑定预约的释放与转实占必须携带持有者上下文
    （任务/批次 ID），防止无任务语境的任意释放；不匹配即不变量破坏，
    fail-closed 拒绝。"""


# --------------------------------------------------------------------------- #
# 1) 计数流 + 2) 磁盘水位（进程内，全后端生效）
# --------------------------------------------------------------------------- #
def save_limited(src_stream, dst_path, limit=None, chunk_size=CHUNK_SIZE):
    """把 src_stream 逐块复制到 dst_path，实际计数，超限立即停止。

    - 不参考任何声明长度（Content-Length / header），只信实读字节；
    - 超过 limit 抛 :class:`RequestTooLarge`，已写的一半文件由调用方清理
      （本函数抛错前尽量删除 dst_path，但调用方仍应兜底 unlink）；
    - 返回实际写入字节数。
    """
    limit = UPLOAD_MAX_REQUEST_BYTES if limit is None else int(limit)
    total = 0
    try:
        with open(dst_path, "wb") as dst:
            while True:
                chunk = src_stream.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise RequestTooLarge(
                        "上传超过单请求字节上限（%d 字节）" % limit)
                dst.write(chunk)
    except Exception:
        try:
            os.unlink(dst_path)
        except OSError:
            pass
        raise
    return total


def check_disk_watermark(dir_path, need_bytes=0, reserved=None):
    """检查 dir_path 所在卷 free - need_bytes 是否仍高于保留水位。

    超水位抛 :class:`DiskWatermarkExceeded`。上传写临时文件前（need=上限的
    保守量或已知大小）与 ZIP 解压过程中（need=已展开量）调用。
    """
    reserved = UPLOAD_RESERVED_FREE_BYTES if reserved is None else int(reserved)
    free = shutil.disk_usage(str(dir_path)).free
    if free - int(need_bytes) < reserved:
        raise DiskWatermarkExceeded(
            "磁盘可用空间低于保留水位（free=%d, need=%d, reserved=%d）"
            % (free, int(need_bytes), reserved))


# --------------------------------------------------------------------------- #
# 3) PG 权威配额 / reservation / 速率
# --------------------------------------------------------------------------- #
def quota_features_available() -> bool:
    """配额 / 在途 / 每小时限流是否可用：仅 postgres。"""
    return platform_features.current_backend() == "postgres"


def quota_applies(ident) -> bool:
    """该身份是否需要走 PG 配额：role=user（受邀账号；owner/本地免登录跳过）。

    ident 为 app.current_identity() 形态的 dict（{"role", "user_id"}）。
    """
    return bool(ident) and ident.get("role") == "user" and bool(ident.get("user_id"))


def _connect():
    import psycopg
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _reservation_out(row) -> dict:
    out = dict(row)
    for k in ("reserved_bytes", "settled_bytes"):
        if out.get(k) is not None:
            out[k] = int(out[k])
    return out


def reservation_holds_capacity(out) -> bool:
    """该预约是否仍持有容量责任：state='reserved'（不看租约/绑定）。

    生命周期模型（0072）：已绑定预约不参加 TTL 回收，租约（expires_at）
    过期只意味着**执行许可**失效，容量仍在账上——「账本与预约表相等」
    之外，活跃任务的容量保障以本函数 + 持有者绑定为準。
    """
    return bool(out) and out.get("state") == "reserved"


def reservation_is_active(out) -> bool:
    """预占是否仍有效：state=reserved 且 expires_at 尚未到期（执行许可）。

    绑定预约经 ``renew_reservation_locked`` 重发租约后恢复 active；
    未绑定预约过期不复活（配额可能已被回收重分配）。
    调用方必须用本函数判定执行许可，不能只看 state；判定**容量责任**
    是否存在用 :func:`reservation_holds_capacity`。
    """
    if not reservation_holds_capacity(out):
        return False
    exp = out.get("expires_at")
    if exp is None:
        return False
    if hasattr(exp, "timestamp"):
        exp = exp.timestamp()
    try:
        return float(exp) > time.time()
    except (TypeError, ValueError):
        return False


def _validate_holder(holder_kind, holder_id, purpose):
    """持有者三元组枚举校验（空/全有 + 种类×用途固定组合）。"""
    given = (holder_kind, holder_id, purpose)
    if all(v in (None, "") for v in given):
        return None
    if any(v in (None, "") for v in given):
        raise ValueError(
            "持有者三元组必须全空或全有（kind/id/purpose）：%r" % (given,))
    holder_kind, holder_id, purpose = (str(holder_kind), str(holder_id),
                                       str(purpose))
    if holder_kind not in HOLDER_KINDS:
        raise ValueError("未知持有者类型：%r（合法：%s）"
                         % (holder_kind, sorted(HOLDER_KINDS)))
    if purpose not in HOLDER_PURPOSES[holder_kind]:
        raise ValueError("持有者 %r 不允许用途 %r（合法：%s）"
                         % (holder_kind, purpose,
                            sorted(HOLDER_PURPOSES[holder_kind])))
    return holder_kind, holder_id, purpose


def _validate_holder_pair(expect_holder):
    """释放/结算的持有者上下文校验：(kind, id) 二元组。"""
    if len(expect_holder) != 2:
        raise ValueError("expect_holder 需为 (holder_kind, holder_id) 二元组")
    kind, hid = expect_holder
    if kind not in HOLDER_KINDS or hid in (None, ""):
        raise ValueError("非法持有者上下文：%r" % (expect_holder,))
    return (str(kind), str(hid))


def reservation_holder_matches(out, holder_kind, holder_id) -> bool:
    """预约当前绑定是否恰为 (holder_kind, holder_id)（未绑定 → False）。"""
    if not out:
        return False
    return ((out.get("holder_kind") or None) == holder_kind
            and (out.get("holder_id") or None) == holder_id)


def _quota_out(row):
    """配额行 → 统一 dict（字节数一律 int）。

    0092 起 ``quota_bytes`` 保持为**有效配额**（基础 + 付费扩容）以兼容既有
    前端/文案；同时给出 ``base_quota_bytes`` 与 ``extra_quota_bytes``。
    """
    base = int(row["quota_bytes"])
    extra = int(row.get("extra_quota_bytes") or 0)
    return {
        "user_id": row["user_id"],
        "base_quota_bytes": base,
        "extra_quota_bytes": extra,
        "quota_bytes": base + extra,
        "used_bytes": int(row["used_bytes"]),
        "reserved_bytes": int(row["reserved_bytes"]),
    }


def get_quota_row(user_id):
    """读取配额行（不存在则按 env 默认建行）；调试/测试/管理读取用。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                    "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
                    (user_id, UPLOAD_USER_QUOTA_BYTES))
                cur.execute(
                    "SELECT user_id, quota_bytes, extra_quota_bytes, "
                    "used_bytes, reserved_bytes "
                    "FROM upload_user_quotas WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
        return _quota_out(row) if row else None
    finally:
        conn.close()


def peek_quota_row(user_id):
    """只读读取配额行；不存在返回 None（**不建行**，用于自助展示）。

    自助用量接口不应因为用户点开账户页就产生配额行；真正需要行时由
    reserve/set 路径惰性创建。``quota_bytes`` 为有效配额（基础 + 扩容），
    另附 ``base_quota_bytes`` / ``extra_quota_bytes``。
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, quota_bytes, extra_quota_bytes, "
                "used_bytes, reserved_bytes "
                "FROM upload_user_quotas WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        return _quota_out(row) if row else None
    finally:
        conn.close()


def _validate_quota_bytes(value):
    """管理员配额入参校验：整数且落在 [MIN, MAX] 字节区间。"""
    if isinstance(value, bool):
        raise ValueError("quota_bytes 需为整数（字节）")
    try:
        quota = int(value)
    except (TypeError, ValueError):
        raise ValueError("quota_bytes 需为整数（字节）")
    if quota < UPLOAD_USER_QUOTA_MIN_BYTES \
            or quota > UPLOAD_USER_QUOTA_MAX_BYTES:
        raise ValueError(
            "quota_bytes 需在 [%d, %d] 字节区间内"
            % (UPLOAD_USER_QUOTA_MIN_BYTES, UPLOAD_USER_QUOTA_MAX_BYTES))
    return quota


def _lock_or_create_quota_row(cur, user_id):
    """锁内确保配额行存在（首次按 env 默认建行），返回 FOR UPDATE 行。"""
    cur.execute(
        "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
        "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
        (user_id, UPLOAD_USER_QUOTA_BYTES))
    cur.execute(
        "SELECT user_id, quota_bytes, extra_quota_bytes, used_bytes, "
        "reserved_bytes FROM upload_user_quotas WHERE user_id = %s "
        "FOR UPDATE", (user_id,))
    return cur.fetchone()


def _write_quota_update(cur, user_id, quota_bytes, *, op, actor_user_id,
                        updated_by, audit_detail=None):
    """锁内写入配额 + 同事务审计（set / restore-default 共用）。

    只改基础容量 ``quota_bytes``；``extra_quota_bytes``（付费扩容）保持
    不变，因此返回的 ``quota_bytes`` 仍是有效配额。
    """
    import share_store_pg

    before = _lock_or_create_quota_row(cur, user_id)
    previous = int(before["quota_bytes"])
    cur.execute(
        "UPDATE upload_user_quotas SET quota_bytes=%s, updated_at=now() "
        "WHERE user_id=%s "
        "RETURNING user_id, quota_bytes, extra_quota_bytes, used_bytes, "
        "reserved_bytes",
        (quota_bytes, user_id))
    out = _quota_out(cur.fetchone())
    detail = dict(audit_detail or {})
    detail.update({
        "op": op,
        "user_id": user_id,
        "previous_quota_bytes": previous,
        "new_quota_bytes": int(quota_bytes),
        "extra_quota_bytes": out["extra_quota_bytes"],
        "used_bytes": out["used_bytes"],
        "reserved_bytes": out["reserved_bytes"],
        "updated_by": updated_by or actor_user_id,
    })
    share_store_pg.record_audit_tx(
        cur, UPLOAD_QUOTA_AUDIT_ACTION,
        actor_user_id=actor_user_id, actor_role="owner",
        target_type="upload_user_quota", target_id=user_id, detail=detail)
    return out


def set_user_quota(user_id, quota_bytes, *, actor_user_id=None,
                   updated_by=None):
    """管理员设置单用户存储配额（绝对值，字节；行不存在则创建）。

    只改 quota_bytes（基础容量）：used_bytes / reserved_bytes 与
    ``extra_quota_bytes``（付费扩容）一律不动。调低到低于已用
    时既有文件不删除、不受影响；后续 reserve/topup 的不等式
    ``used + reserved + nbytes <= quota`` 自然拒绝新增上传。边界见
    ``UPLOAD_USER_QUOTA_MIN_BYTES`` / ``UPLOAD_USER_QUOTA_MAX_BYTES``。
    写入与审计同一事务（action=``UPLOAD_QUOTA_AUDIT_ACTION``）。
    """
    quota = _validate_quota_bytes(quota_bytes)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _write_quota_update(
                    cur, user_id, quota, op="set_user_quota",
                    actor_user_id=actor_user_id, updated_by=updated_by)
    finally:
        conn.close()


def restore_user_quota_default(user_id, *, actor_user_id=None,
                               updated_by=None):
    """管理员「恢复默认」：把该用户配额显式改为**当时**的 env 默认值。

    与 :func:`set_user_quota` 同款写入 + 同事务审计；used/reserved 不动。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return _write_quota_update(
                    cur, user_id, int(UPLOAD_USER_QUOTA_BYTES),
                    op="restore_user_quota_default",
                    actor_user_id=actor_user_id, updated_by=updated_by,
                    audit_detail={"default_quota_bytes":
                                  int(UPLOAD_USER_QUOTA_BYTES)})
    finally:
        conn.close()


def reserve_upload_locked(cur, user_id, nbytes, *, inflight_limit=None,
                          hourly_limit=None, rid=None, holder_kind=None,
                          holder_id=None, purpose=None, origin="admission"):
    """``reserve_upload`` 的 cursor 变体：在调用方已打开的事务内预占。

    COS 摄取准入（ingestion_store）需要「锁 job 行 → 本地配额预占 → COS 池
    预约」同事务原子完成，故抽出本函数；行为与 ``reserve_upload`` 逐句一致
    （锁配额行 → 惰性回收 → 在途/每小时判定 → 配额判定 → 插行 + 累加），
    ``rid`` 可由调用方注入（测试确定性）。返回 ``_reservation_out`` dict。

    生命周期（0072）：``holder_*`` 三元组给出时**准入即绑定**（无未绑定
    窗口——能同事务创建任务与预约的入口直接绑定，不制造中间状态）；
    全空 = 准备期未绑定预约（任何 writer 启动前必须 ``bind_reservation_
    locked`` 绑定，未绑定且未开始 I/O 的短预约才保留 TTL 回收能力）。
    """
    nbytes = int(nbytes)
    if nbytes <= 0:
        raise ValueError("nbytes 需为正整数")
    holder = _validate_holder(holder_kind, holder_id, purpose)
    inflight_limit = UPLOAD_MAX_INFLIGHT if inflight_limit is None else inflight_limit
    hourly_limit = (UPLOAD_HOURLY_REQUEST_LIMIT if hourly_limit is None
                    else hourly_limit)
    rid = rid or ("upr_" + secrets.token_hex(12))
    cur.execute(
        "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
        "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
        (user_id, UPLOAD_USER_QUOTA_BYTES))
    # 锁配额行：同用户并发 reserve 串行化（docs §3.3-3）；有效配额 =
    # 基础容量 + 付费扩容（0092）。
    cur.execute(
        "SELECT quota_bytes, extra_quota_bytes, used_bytes, reserved_bytes "
        "FROM upload_user_quotas WHERE user_id = %s FOR UPDATE",
        (user_id,))
    q = cur.fetchone()
    quota, used, reserved = (int(q["quota_bytes"])
                             + int(q["extra_quota_bytes"] or 0),
                             int(q["used_bytes"]),
                             int(q["reserved_bytes"]))
    # 惰性回收**未绑定**过期预占（准备期崩溃兜底；锁内执行防并发双扣）。
    # 生命周期模型（0072 / R11）：已绑定（holder_id 非空）的预约是任务
    # 持有的容量责任，**不参加 TTL 回收**——心跳停止不代表字节消失，
    # 终止与释放只能走「清理确认后释放」编排。减账依据实际转换的行
    # （UPDATE ... RETURNING 逐行合计，R8 口径——不用锁前可能漂移的 SUM）。
    cur.execute(
        "UPDATE upload_reservations SET state='released', "
        "settled_at=now(), settled_bytes=0, updated_at=now() "
        "WHERE user_id=%s AND state='reserved' "
        "AND holder_id IS NULL AND expires_at <= now() "
        "RETURNING reserved_bytes", (user_id,))
    expired = sum(int(r["reserved_bytes"]) for r in cur.fetchall())
    if expired:
        cur.execute(
            "UPDATE upload_user_quotas SET reserved_bytes = "
            "GREATEST(0, reserved_bytes - %s), updated_at=now() "
            "WHERE user_id=%s", (expired, user_id))
        reserved = max(0, reserved - expired)
    # 在途上限（回收后的真实在途责任数）。口径（plan §4.2）：已绑定责任
    # 不看租约（清理中的任务可不占执行槽但**仍占容量**——upload_cleanup_
    # pending 引用的责任豁免执行槽，容量仍由 reserved_bytes 持有）；未
    # 绑定预约仅租约内计入（准备期不得绕过并发上限）。
    cur.execute(
        "SELECT COUNT(*)::int AS n FROM upload_reservations r "
        "WHERE r.user_id=%s AND r.state='reserved' "
        "AND (r.holder_id IS NOT NULL OR r.expires_at > now()) "
        "AND NOT EXISTS (SELECT 1 FROM upload_cleanup_pending p "
        " WHERE p.reservation_id = r.reservation_id)",
        (user_id,))
    if int(cur.fetchone()["n"]) >= inflight_limit:
        raise InflightLimitExceeded(
            "在途上传数已达上限（%d）" % inflight_limit)
    # 每小时请求数上限（计尝试次数：不论终态的近 1 小时行数；仅正常准入
    # ——origin='reconcile' 的核账补建不算一次用户上传，plan §4.1）。
    cur.execute(
        "SELECT COUNT(*)::int AS n FROM upload_reservations "
        "WHERE user_id=%s AND reserved_at > now() - interval '1 hour' "
        "AND origin='admission'",
        (user_id,))
    if int(cur.fetchone()["n"]) >= hourly_limit:
        raise RateLimitExceeded(
            "每小时上传请求数已达上限（%d）" % hourly_limit)
    # 配额条件判定
    if used + reserved + nbytes > quota:
        raise QuotaExceeded(
            "用户存储配额不足（quota=%d, used=%d, reserved=%d, "
            "need=%d）" % (quota, used, reserved, nbytes))
    cur.execute(
        "INSERT INTO upload_reservations "
        "(reservation_id, user_id, reserved_bytes, state, "
        " reserved_at, expires_at, holder_kind, holder_id, purpose, origin) "
        "VALUES (%s, %s, %s, 'reserved', now(), "
        " now() + make_interval(secs => %s), %s, %s, %s, %s)",
        (rid, user_id, nbytes, UPLOAD_RESERVATION_TTL_SECONDS,
         holder[0] if holder else None,
         holder[1] if holder else None,
         holder[2] if holder else None,
         "reconcile" if origin == "reconcile" else "admission"))
    cur.execute(
        "UPDATE upload_user_quotas SET reserved_bytes = "
        "reserved_bytes + %s, updated_at=now() WHERE user_id=%s",
        (nbytes, user_id))
    cur.execute(
        "SELECT * FROM upload_reservations WHERE reservation_id=%s",
        (rid,))
    return _reservation_out(cur.fetchone())


def reserve_upload(user_id, nbytes, *, inflight_limit=None,
                   hourly_limit=None, holder_kind=None, holder_id=None,
                   purpose=None):
    """原子预占 nbytes 字节。返回 reservation dict。

    单事务内：锁配额行 → 惰性回收过期**未绑定**预占 → 在途/每小时判定 →
    配额条件判定 → 插入 reservation + 累加 reserved_bytes。任一判定失败
    整体回滚（不会先扣一个维度再失败）。``holder_*`` 三元组给出时准入即
    绑定（0072；无未绑定窗口）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return reserve_upload_locked(
                    cur, user_id, nbytes, inflight_limit=inflight_limit,
                    hourly_limit=hourly_limit, holder_kind=holder_kind,
                    holder_id=holder_id, purpose=purpose)
    finally:
        conn.close()


def bind_reservation_locked(cur, reservation_id, holder_kind, holder_id,
                            purpose):
    """把准备期预约绑定到持有者（writer 启动前；与任务行创建同事务）。

    生命周期（0072 / plan §4.1）：未绑定预约只存在于「尚未产生字节」的
    准备阶段；任何 writer 启动（写第一个字节）前必须完成绑定——绑定后
    预约不参加 TTL 回收，容量责任与任务同进退。

    CAS 语义（幂等 + fail-closed）：
      - reserved 且未绑定 → 绑定（返回绑定后行）；
      - 已绑定**同一**持有者 → 幂等 no-op（重试/恢复复用）；
      - 已绑定其它持有者 / 已 released/consumed / 不存在 →
        :class:`ReservationHolderMismatch` / :class:`ReservationInvalid`
        （不变量破坏，不猜不改）。
    """
    holder = _validate_holder(holder_kind, holder_id, purpose)
    if holder is None:
        raise ValueError("bind_reservation_locked 需要完整持有者三元组")
    cur.execute(
        "SELECT user_id FROM upload_reservations WHERE reservation_id=%s",
        (reservation_id,))
    loc = cur.fetchone()
    if loc is None:
        raise ReservationInvalid("预占不存在：%r" % reservation_id)
    if loc["user_id"]:
        _lock_quota_row(cur, loc["user_id"])
    cur.execute(
        "SELECT state, holder_kind, holder_id, purpose "
        "FROM upload_reservations WHERE reservation_id=%s FOR UPDATE",
        (reservation_id,))
    r = cur.fetchone()
    if r is None:
        raise ReservationInvalid("预占不存在：%r" % reservation_id)
    if r["holder_kind"] is not None:
        if (r["holder_kind"], r["holder_id"], r["purpose"]) == holder:
            cur.execute(
                "SELECT * FROM upload_reservations "
                "WHERE reservation_id=%s", (reservation_id,))
            return _reservation_out(cur.fetchone())
        raise ReservationHolderMismatch(
            "预占已绑定其它持有者（%r/%r → 试图绑 %r/%r）——不变量破坏"
            % (r["holder_kind"], r["holder_id"], holder[0], holder[1]))
    if r["state"] != "reserved":
        raise ReservationInvalid(
            "预占已 %s，不能绑定：%r" % (r["state"], reservation_id))
    cur.execute(
        "UPDATE upload_reservations SET holder_kind=%s, holder_id=%s, "
        "purpose=%s, updated_at=now() WHERE reservation_id=%s",
        (holder[0], holder[1], holder[2], reservation_id))
    cur.execute(
        "SELECT * FROM upload_reservations WHERE reservation_id=%s",
        (reservation_id,))
    return _reservation_out(cur.fetchone())


def topup_reservation(reservation_id, extra_bytes):
    """ZIP 展开总量超过预占时的原子补占（reserved 条件加码）。

    R10 锁序统一：quota 行 → reservation 行（与准入/释放/转实占/登记
    同序；原 reservation → quota 与 consume 并发可死锁）。首查只定位
    user_id；**锁内**重读预约状态/有效期/字节与配额余量后再判定——
    不用锁前快照。配额不足抛 QuotaExceeded，整体回滚。
    """
    extra_bytes = int(extra_bytes)
    if extra_bytes <= 0:
        return get_reservation(reservation_id)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # 定位 user_id（行上不可变；不承担状态判定）
                cur.execute(
                    "SELECT user_id FROM upload_reservations "
                    "WHERE reservation_id=%s", (reservation_id,))
                loc = cur.fetchone()
                if loc is None:
                    raise ReservationInvalid("预占不存在：%r"
                                             % reservation_id)
                if not loc["user_id"]:
                    raise ReservationInvalid("预占无归属用户（不补占）")
                _lock_quota_row(cur, loc["user_id"])
                # 过期/状态判定放 SQL（expires_at 是 timestamptz，与 now()
                # 同钟）；FOR UPDATE 在配额锁之后取得——锁内权威读。
                cur.execute(
                    "SELECT user_id, reserved_bytes FROM upload_reservations "
                    "WHERE reservation_id=%s AND state='reserved' "
                    "AND expires_at > now() FOR UPDATE",
                    (reservation_id,))
                r = cur.fetchone()
                if r is None:
                    raise ReservationInvalid("预占不存在、已结算或已过期")
                cur.execute(
                    "SELECT quota_bytes, extra_quota_bytes, used_bytes, "
                    "reserved_bytes FROM upload_user_quotas WHERE user_id=%s",
                    (r["user_id"],))
                q = cur.fetchone()
                quota, used, reserved = (
                    int(q["quota_bytes"]) + int(q["extra_quota_bytes"] or 0),
                    int(q["used_bytes"]),
                    int(q["reserved_bytes"]))
                if used + reserved + extra_bytes > quota:
                    raise QuotaExceeded(
                        "用户存储配额不足（需补占 %d 字节）" % extra_bytes)
                cur.execute(
                    "UPDATE upload_reservations SET reserved_bytes = "
                    "reserved_bytes + %s, expires_at = now() + "
                    "make_interval(secs => %s), updated_at=now() "
                    "WHERE reservation_id=%s",
                    (extra_bytes, UPLOAD_RESERVATION_TTL_SECONDS, reservation_id))
                cur.execute(
                    "UPDATE upload_user_quotas SET reserved_bytes = "
                    "reserved_bytes + %s, updated_at=now() WHERE user_id=%s",
                    (extra_bytes, r["user_id"]))
                cur.execute(
                    "SELECT * FROM upload_reservations WHERE reservation_id=%s",
                    (reservation_id,))
                row = cur.fetchone()
        return _reservation_out(row)
    finally:
        conn.close()


def get_reservation(reservation_id):
    """读取 reservation 行（不存在返回 None）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM upload_reservations WHERE reservation_id=%s",
                    (reservation_id,))
                row = cur.fetchone()
        return _reservation_out(row) if row else None
    finally:
        conn.close()


def renew_reservation_locked(cur, reservation_id, ttl_seconds=None):
    """``renew_reservation`` 的 cursor 变体（调用方事务内续执行租约）。

    COS 容量调度器需要「锁 job 行 → 重验状态 → 续租」同事务，故抽出。

    生命周期语义（0072 / R11 P1 修复，plan §4.2「核验并续执行租约」）：
      - **已绑定**预约（holder_id 非空）：容量由任务持有、从不被 TTL 回收，
        租约过期只意味着执行许可失效——**重发租约**（expires_at=now()+ttl），
        不重新准入、不换 rid、不产生新的每小时准入计数。业务绝对截止
        （任务 TTL / job_deadline_at）由各通道 sweep 独立强制，租约不
        无限延长业务的寿命。
      - **未绑定**预约：租约过期不复活（配额可能已被回收重分配——与
        原语义一致）；
      - consumed/released 幂等 no-op，返回当前行；不存在 → None。
    """
    ttl = (UPLOAD_RESERVATION_TTL_SECONDS if ttl_seconds is None
           else int(ttl_seconds))
    # R10 锁序统一：quota 行 → reservation 行。renew 自身不改配额，但调用
    # 方事务（发布 precheck、COS 常驻续租）在 renew 之后可能继续 acquire/
    # release/consume——统一先取配额锁，整个调用链不再出现
    # 「先锁预约、后取配额」的倒置（与准入回收/登记/释放/转实占同序）。
    cur.execute(
        "SELECT user_id FROM upload_reservations WHERE reservation_id=%s",
        (reservation_id,))
    _loc = cur.fetchone()
    if _loc is not None and _loc["user_id"]:
        _lock_quota_row(cur, _loc["user_id"])
    cur.execute(
        "SELECT user_id, reserved_bytes, state, holder_id FROM "
        "upload_reservations WHERE reservation_id=%s FOR UPDATE",
        (reservation_id,))
    r = cur.fetchone()
    if r is None:
        return None
    if r["state"] == "reserved":
        # 绑定预约：重发租约（容量从未离开账本，重发安全）；未绑定预约：
        # 仅未过期时后移（过期不复活——配额可能已被回收重分配）。
        # 过期判定放 SQL（与 now() 同钟，防应用/DB 时钟偏差）。
        cur.execute(
            "UPDATE upload_reservations SET expires_at = now() + "
            "make_interval(secs => %s), updated_at=now() "
            "WHERE reservation_id=%s AND state='reserved' "
            "AND (holder_id IS NOT NULL OR expires_at > now())",
            (ttl, reservation_id))
    cur.execute(
        "SELECT * FROM upload_reservations WHERE reservation_id=%s",
        (reservation_id,))
    return _reservation_out(cur.fetchone())


def renew_reservation(reservation_id, ttl_seconds=None):
    """续执行租约（Upload V2 §3.2.4 + 生命周期 0072）：把 reserved 预占的
    expires_at 后移 now()+ttl。

    与 ``topup_reservation``（补字节）互补：本函数不改 reserved_bytes。已
    绑定预约的容量**从不**被 TTL 回收，租约只控制执行许可——绑定预约租约
    过期后重发（见 ``renew_reservation_locked``）；未绑定预约过期不复活。

    单事务内按统一锁序（quota → reservation）判定：

    - ``reserved`` 且（未绑定未过期 ∨ 已绑定）→ UPDATE expires_at（返回
      续租后的行；绑定预约「已过期未回收」也重发——容量仍在账上）；
    - ``reserved`` 且未绑定已过期（尚未被惰性回收）→ **不复活**（此刻
      配额可能已被回收重分配，复活会双占），返回当前行，调用方据此判定
      预占失效；
    - ``consumed`` / ``released`` → 幂等 no-op，返回当前行；
    - 不存在 → None。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return renew_reservation_locked(cur, reservation_id, ttl_seconds)
    finally:
        conn.close()


def _lock_quota_row(cur, user_id):
    """统一锁序（R9 复核修复 P1 的锁序审计）：全部预约状态路径按
    ``upload_user_quotas 行锁 → upload_reservations 行锁`` 全序执行
    （准入回收/待清理登记已是此序；release/consume 原为反序，存在与
    准入回收同预约并发时的锁序倒置死锁面）。仅锁行不返回数据语义。"""
    cur.execute(
        "SELECT reserved_bytes FROM upload_user_quotas "
        "WHERE user_id=%s FOR UPDATE", (user_id,))


def release_reservation_locked(cur, reservation_id, *, expect_holder=None):
    """``release_reservation`` 的 cursor 变体（调用方事务内释放）。

    生命周期（0072 / plan §4.2「结算或确认清理后释放」）：

    - **未绑定**预约（准备期放弃：V1/V2 受理前失败）→ 直接释放；
    - **已绑定**预约 → 必须携带 ``expect_holder=(kind, id)`` 且与行内绑定
      一致（清理确认收口 / 批次闭班收口的任务语境），否则
      :class:`ReservationHolderMismatch`——不保留无任务语境、可任意释放
      绑定预约的入口；
    - 已 consumed/released 幂等 no-op（返回其状态）。

    锁序（R10 统一）：quota 行 → reservation 行。
    """
    holder = _validate_holder_pair(expect_holder) if expect_holder else None
    cur.execute(
        "SELECT user_id FROM upload_reservations WHERE reservation_id=%s",
        (reservation_id,))
    loc = cur.fetchone()
    if loc is not None and loc["user_id"]:
        _lock_quota_row(cur, loc["user_id"])  # 统一锁序（quota → 行）
    cur.execute(
        "SELECT user_id, reserved_bytes, state, holder_kind, holder_id "
        "FROM upload_reservations WHERE reservation_id=%s FOR UPDATE",
        (reservation_id,))
    r = cur.fetchone()
    if r is None:
        return None
    if r["state"] != "reserved":
        return {"reservation_id": reservation_id, "state": r["state"]}
    if r["holder_id"] is not None:
        if holder is None:
            raise ReservationHolderMismatch(
                "绑定预约（%r/%r）的释放需要持有者上下文（清理确认收口）"
                % (r["holder_kind"], r["holder_id"]))
        if (r["holder_kind"], r["holder_id"]) != (holder[0], holder[1]):
            raise ReservationHolderMismatch(
                "释放的持有者不符（行=%r/%r，调用=%r/%r）——不变量破坏"
                % (r["holder_kind"], r["holder_id"], holder[0], holder[1]))
    cur.execute(
        "UPDATE upload_reservations SET state='released', "
        "settled_at=now(), settled_bytes=0, updated_at=now() "
        "WHERE reservation_id=%s", (reservation_id,))
    cur.execute(
        "UPDATE upload_user_quotas SET reserved_bytes = "
        "GREATEST(0, reserved_bytes - %s), updated_at=now() "
        "WHERE user_id=%s", (int(r["reserved_bytes"]), r["user_id"]))
    return {"reservation_id": reservation_id, "state": "released"}


def release_reservation(reservation_id, *, expect_holder=None):
    """失败释放（未绑定准备期预约直接释放；绑定预约需 ``expect_holder``）。

    已 consumed/released 幂等 no-op。绑定预约（任务持有容量）不得无语境
    释放——只能经「清理确认后释放」的持有者上下文路径（V1/V2
    ``_upload_v2_cleanup_confirmed``、COS ``confirm_local_cleanup``、百度
    批次闭班收口），违反即 :class:`ReservationHolderMismatch`（fail-closed，
    捕获不变量破坏）。R10 锁序：状态转换与减账只有 ``release_reservation_
    locked`` 一份实现（quota 行 → reservation 行），本函数只开事务委托。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return release_reservation_locked(
                    cur, reservation_id, expect_holder=expect_holder)
    finally:
        conn.close()


def consume_reservation_locked(cur, reservation_id, actual_bytes,
                               *, expect_holder=None):
    """在调用方已打开的事务/cursor 内转实占（与任务收口同事务）。

    生命周期（0072）：

    - **已绑定**预约：到期判定不适用（容量由任务持有、从不被 TTL 回收，
      租约过期只影响执行许可——合法完成任务结算不受租约过期拒绝）；
      绑定行必须携带 ``expect_holder=(kind, id)`` 且一致（发布结算的任务
      语境）；
    - **未绑定**预约：保持原到期判定（过期不能转实占——配额可能已被
      回收重分配）；
    - 幂等：已 consumed 的行再次 consume 返回现状，不重复累计。
    """
    actual_bytes = int(actual_bytes)
    holder = _validate_holder_pair(expect_holder) if expect_holder else None
    cur.execute(
        "SELECT user_id FROM upload_reservations WHERE reservation_id=%s",
        (reservation_id,))
    loc = cur.fetchone()
    if loc is not None and loc["user_id"]:
        _lock_quota_row(cur, loc["user_id"])  # 统一锁序（quota → 行）
    cur.execute(
        "SELECT user_id, reserved_bytes, state, holder_kind, holder_id "
        "FROM upload_reservations WHERE reservation_id=%s FOR UPDATE",
        (reservation_id,))
    r = cur.fetchone()
    if r is None:
        raise ReservationInvalid("预占不存在：%r" % reservation_id)
    if r["state"] == "consumed":
        return {"reservation_id": reservation_id, "state": "consumed"}
    if r["state"] != "reserved":
        raise ReservationInvalid(
            "预占已释放，不能转实占：%r" % reservation_id)
    if r["holder_id"] is not None:
        if holder is None:
            raise ReservationHolderMismatch(
                "绑定预约（%r/%r）的结算需要持有者上下文（任务语境）"
                % (r["holder_kind"], r["holder_id"]))
        if (r["holder_kind"], r["holder_id"]) != (holder[0], holder[1]):
            raise ReservationHolderMismatch(
                "结算的持有者不符（行=%r/%r，调用=%r/%r）——不变量破坏"
                % (r["holder_kind"], r["holder_id"], holder[0], holder[1]))
    else:
        cur.execute(
            "SELECT 1 FROM upload_reservations WHERE reservation_id=%s "
            "AND expires_at > now()", (reservation_id,))
        if cur.fetchone() is None:
            raise ReservationInvalid(
                "预占已过期，不能转实占：%r" % reservation_id)
    cur.execute(
        "UPDATE upload_reservations SET state='consumed', "
        "settled_at=now(), settled_bytes=%s, updated_at=now() "
        "WHERE reservation_id=%s", (actual_bytes, reservation_id))
    cur.execute(
        "UPDATE upload_user_quotas SET "
        "reserved_bytes = GREATEST(0, reserved_bytes - %s), "
        "used_bytes = used_bytes + %s, updated_at=now() "
        "WHERE user_id=%s",
        (int(r["reserved_bytes"]), actual_bytes, r["user_id"]))
    return {"reservation_id": reservation_id, "state": "consumed",
            "settled_bytes": actual_bytes}


def consume_reservation(reservation_id, actual_bytes, *, expect_holder=None):
    """成功转实占：reserved → consumed，reserved 转 used（按实际字节数）。

    幂等：已 consumed 的行再次 consume 返回现状，不重复累计。绑定预约
    必须 ``expect_holder``（发布结算的任务语境，0072）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return consume_reservation_locked(
                    cur, reservation_id, actual_bytes,
                    expect_holder=expect_holder)
    finally:
        conn.close()


def record_reconciled_residual_locked(cur, user_id, nbytes, *,
                                      holder_kind=None, holder_id=None,
                                      purpose=None, rid=None):
    """维护专用补记入口（R12 §3.5；**仅核账脚本调用，不进 Web API**）。

    与正常准入共享底层唯一财务记账实现（INSERT reservation + reserved_bytes
    累加），但**不执行**「新上传」的额度/并发/每小时判定——既有字节不是
    新上传申请；配额不足也按实补记（quota_bytes 不变，超额由正常准入的
    guard 继续拒绝）。``origin='reconcile'``（不计每小时准入数）；可同时
    绑定持有者（与任务/pending 指针同一事务更新）。旧 released 行保持
    不变（新责任是新行）。返回 ``_reservation_out`` dict。

    调用前提（脚本保证）：受信维护计划、任务已停止、路径与字节证据已
    复核。正常 ``reserve_upload`` 无法经任何参数走到本语义（公开 reserve
    不接受 origin='reconcile'）。
    """
    nbytes = int(nbytes)
    if nbytes <= 0:
        raise ValueError("补记字节数需为正整数")
    holder = _validate_holder(holder_kind, holder_id, purpose)
    rid = rid or ("upr_" + secrets.token_hex(12))
    cur.execute(
        "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
        "VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
        (user_id, UPLOAD_USER_QUOTA_BYTES))
    _lock_quota_row(cur, user_id)
    cur.execute(
        "INSERT INTO upload_reservations "
        "(reservation_id, user_id, reserved_bytes, state, reserved_at, "
        " expires_at, holder_kind, holder_id, purpose, origin) "
        "VALUES (%s, %s, %s, 'reserved', now(), "
        " now() + make_interval(secs => %s), %s, %s, %s, 'reconcile')",
        (rid, user_id, nbytes, UPLOAD_RESERVATION_TTL_SECONDS,
         holder[0] if holder else None, holder[1] if holder else None,
         holder[2] if holder else None))
    cur.execute(
        "UPDATE upload_user_quotas SET reserved_bytes = reserved_bytes + %s, "
        "updated_at=now() WHERE user_id=%s", (nbytes, user_id))
    cur.execute(
        "SELECT * FROM upload_reservations WHERE reservation_id=%s", (rid,))
    return _reservation_out(cur.fetchone())


def add_used_bytes_locked(cur, user_id, nbytes):
    """转换产出等额外入账的 cursor 变体（调用方事务内直接累加 used_bytes）。

    生命周期（plan §8「结算只有一份实现」）：used_bytes 的财务 SQL 唯一
    实现收口在本模块——conversion_store 等无预约通道的结算在本原语上
    调用（幂等键仍由调用方的业务 CAS 保证，如 conversion 的
    canonical_settled_bytes）。空 user_id（owner/本地免登录）与 nbytes<=0
    为 no-op。"""
    if not user_id or int(nbytes) <= 0:
        return False
    # 不 ensure 行：配额行由 reserve 路径惰性建；owner/无行用户
    # （upload_user_quotas 有 users 外键）静默 0 行——与删除退款原语
    # refund_used_bytes_locked 同口径。
    cur.execute(
        "UPDATE upload_user_quotas SET used_bytes = used_bytes + %s, "
        "updated_at=now() WHERE user_id=%s", (int(nbytes), user_id))
    return cur.rowcount > 0


def add_used_bytes(user_id, nbytes):
    """转换产出等额外入账：直接累加 used_bytes（无 reservation）。

    owner / 空 user_id 跳过。nbytes<=0 为 no-op。
    """
    if not user_id or int(nbytes) <= 0:
        return None
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                return add_used_bytes_locked(cur, user_id, nbytes)
    finally:
        conn.close()


def refund_used_bytes_locked(cur, user_id, nbytes):
    """P3 删除结算原语（合同 §5 / R-12）：在调用方已打开的事务内幂等减少
    used_bytes。

    幂等键 = 状态机 CAS 本身（deleting→deleted 只成功一次，减账与 CAS 同
    事务——重复 DELETE/worker 重试时 CAS 已不匹配，不再进入本函数）。空
    user_id（owner/本地免登录无配额行）与 nbytes<=0 为 no-op；行缺失静默
    （quota 行惰性建，删除路径不强制存在）。GREATEST(0,…) 兜底防负。
    """
    if not user_id or int(nbytes) <= 0:
        return False
    cur.execute(
        "UPDATE upload_user_quotas SET "
        "used_bytes = GREATEST(0, used_bytes - %s), updated_at=now() "
        "WHERE user_id=%s", (int(nbytes), user_id))
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# 默认值依据汇总（上线前复核清单；详见各常量处注释）
# --------------------------------------------------------------------------- #
DEFAULTS_RATIONALE = {
    "UPLOAD_MAX_REQUEST_BYTES": (
        "10 GiB：覆盖 TCGA SVS（常见 0.5–2.5 GiB）与 MRXS+伴侣目录（数 GiB）"
        "的极大 specimen；上线前按真实分布 P99 收紧，且边缘代理 body 上限"
        "必须同步同值"),
    "UPLOAD_USER_QUOTA_BYTES": (
        "10 GiB：新用户默认；早期真实用户由 0091 迁移提到 50 GiB。管理员"
        "可经 admin API 按用户调高（硬顶 1 TiB）"),
    "UPLOAD_RESERVED_FREE_BYTES": (
        "20 GiB：为 PG 数据目录、日志与解压暂存保留的余量；按磁盘规格与"
        "PG/日志实际增速复核"),
    "UPLOAD_MAX_INFLIGHT": "3：正常用户不会同时传 3 个以上大文件",
    "UPLOAD_HOURLY_REQUEST_LIMIT": "60：受邀协作场景远够用，压制重试风暴",
    "UPLOAD_RESERVATION_TTL_SECONDS": (
        "1800：10 GiB 在 5 MB/s 慢链路约 35 分钟的略宽上界；进程崩溃后由"
        "下一次 reserve 惰性回收"),
}
