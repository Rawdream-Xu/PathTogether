# -*- coding: utf-8 -*-
"""COS 直传配置（docs/cos-direct-upload-audit-plan.md §6.1/§6.3 环境变量唯一来源）。

约定：

- 全部为十进制整数字节（D3）；``GB`` 一律十进制，禁止 ``1024**3``。
- 只从进程 env 读取，默认值即合同首发值；缺 secret/bucket 时 COS 整体
  不可用（capability 保持 off，worker 空转并周期告警），绝不部分启用。
- 秘密只经 env 注入（容器 env / secret file 由部署层接线），本模块与
  任何日志/事件/证据都不输出 SecretId/SecretKey 值。
"""

import os


def _env_int(name, default):
    """整型 env（空/非法回落 default；upload_guard._env_int 同款语义）。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_str(name, default=""):
    return (os.environ.get(name) or "").strip() or default


# --------------------------------------------------------------------------- #
# 暂存池（§6.1；与 cos_pool_state 单行一致——应用层以行值为准入权威，
# 此处 env 值在 ensure_pool_state 时校验并同步进行）
# --------------------------------------------------------------------------- #
COS_POOL_CAPACITY_BYTES = _env_int("COS_POOL_CAPACITY_BYTES", 10_000_000_000)
COS_POOL_SAFETY_BYTES = _env_int("COS_POOL_SAFETY_BYTES", 500_000_000)
#: 派生准入上限 = capacity - safety，不单独配置（合同 §6.1 表）。
COS_POOL_ADMISSION_BYTES = COS_POOL_CAPACITY_BYTES - COS_POOL_SAFETY_BYTES

COS_MAX_ACTIVE_UPLOADS_PER_IDENTITY = _env_int(
    "COS_MAX_ACTIVE_UPLOADS_PER_IDENTITY", 1)
COS_MAX_ACTIVE_UPLOADS_GLOBAL = _env_int("COS_MAX_ACTIVE_UPLOADS_GLOBAL", 2)
COS_MAX_WAITING_PER_IDENTITY = _env_int("COS_MAX_WAITING_PER_IDENTITY", 1)
COS_WAITING_MAX_AGE_SECONDS = _env_int("COS_WAITING_MAX_AGE_SECONDS", 86_400)
COS_JOB_MAX_AGE_SECONDS = _env_int("COS_JOB_MAX_AGE_SECONDS", 259_200)

# --------------------------------------------------------------------------- #
# 客户端活性（upload-liveness 修复：vanish 的浏览器不得长期占用活跃名额
# 与池预约；浏览器 waiting 轮询 5s、分块上传心跳 60s）
# --------------------------------------------------------------------------- #
#: owner 触点（状态轮询/parts/sign/upload-complete）刷新 client_seen_at 的
#: 节流下限（秒）——单条 UPDATE，幂等且廉价。
COS_CLIENT_SEEN_TOUCH_SECONDS = _env_int("COS_CLIENT_SEEN_TOUCH_SECONDS", 30)
#: waiting 准入闸：client_seen_at 静默超过该秒数的等待任务不再准入，就地
#: 终止（fail_code=client_gone）。客户端等待期 5s 轮询，120s ≈ 24 个轮询
#: 周期。0/负 = 关闭该闸（不推荐）。
COS_WAITING_CLIENT_SEEN_TIMEOUT_SECONDS = _env_int(
    "COS_WAITING_CLIENT_SEEN_TIMEOUT_SECONDS", 120)
#: uploading 失活 sweep：client_seen_at 静默超过该秒数的上传任务，经 COS
#: ListParts 活性复核（任一分块在阈值内被修改/分块数增长 → 视为在场并
#: 刷新；ListParts 错误 → 本轮跳过，绝不因对端错误终止）后终止
#: （fail_code=client_idle_timeout）。0/负 = 关闭 sweep（不推荐）。
COS_UPLOADING_CLIENT_IDLE_SECONDS = _env_int(
    "COS_UPLOADING_CLIENT_IDLE_SECONDS", 1800)

# --------------------------------------------------------------------------- #
# 分块与预签名（A-presign-parts；§3.1 PoC 起点，非生产定值）
# --------------------------------------------------------------------------- #
#: 分块字节数（十进制起点 32_000_000，合同 §3.1）。
COS_PART_BYTES = _env_int("COS_PART_BYTES", 32_000_000)
#: UploadPart 预签名 TTL（秒）。短 TTL + 批量续签；停发 ≠ 撤销已发 URL。
COS_PART_URL_TTL_SECONDS = _env_int("COS_PART_URL_TTL_SECONDS", 900)
#: 单次 parts/sign 批量上限（按少量待传分块批量签发）。
COS_SIGN_BATCH_MAX_PARTS = _env_int("COS_SIGN_BATCH_MAX_PARTS", 8)
#: 每任务每分钟 parts/sign 批次上限（授权/签名速率硬停）。
COS_SIGN_BATCHES_PER_MINUTE = _env_int("COS_SIGN_BATCHES_PER_MINUTE", 30)
#: 每文件浏览器并发 UploadPart 数（PoC 起点 3，§3.1）。
COS_UPLOAD_PART_CONCURRENCY = _env_int("COS_UPLOAD_PART_CONCURRENCY", 3)

# --------------------------------------------------------------------------- #
# 调度器与 worker（§6.3）
# --------------------------------------------------------------------------- #
#: 容量调度器扫描间隔；必须 < reservation TTL 的 1/3（1800/3=600）。
COS_SCHEDULER_INTERVAL_SECONDS = _env_int("COS_SCHEDULER_INTERVAL_SECONDS", 60)
#: worker 执行租约秒数（领取后未收口即失租可被重领）。
COS_WORKER_LEASE_SECONDS = _env_int("COS_WORKER_LEASE_SECONDS", 900)
#: worker 轮询间隔。
COS_WORKER_INTERVAL_SECONDS = _env_int("COS_WORKER_INTERVAL_SECONDS", 5)
#: 清理重试退避基数（指数）与上限次数。
COS_CLEANUP_RETRY_BASE_SECONDS = _env_int(
    "COS_CLEANUP_RETRY_BASE_SECONDS", 60)
COS_CLEANUP_MAX_ATTEMPTS = _env_int("COS_CLEANUP_MAX_ATTEMPTS", 10)
#: 每任务实际传输字节上限（含失败/重传）= 声明大小 × 倍数；超限硬停下载。
COS_DOWNLOAD_WIRE_BUDGET_MULTIPLIER = _env_int(
    "COS_DOWNLOAD_WIRE_BUDGET_MULTIPLIER", 2)
#: 全局月下载预算（十进制字节；0 = 不设预算，仅统计）。费用硬停口径之一。
COS_MONTHLY_DOWNLOAD_BUDGET_BYTES = _env_int(
    "COS_MONTHLY_DOWNLOAD_BUDGET_BYTES", 0)

# --------------------------------------------------------------------------- #
# 下载传输通道（docs/cos-fast-goapi-relay-design-20261013.md；
# 线合同 gokapi-relay/docs/slide-relay-contract.md §PathTogether env）
#
# ``direct``（默认）走官方 SDK 直连 COS；``fast`` 经腾讯云 fast 纯流式转发
# （同地域内网回源，节约公网下行费）。**fast 故障时不自动回退 direct**——
# 回退是显式运维动作。未知取值 fail-closed（不下载、告警）。
#
# 这些常量在调用点按属性读取（测试用 ``monkeypatch.setattr(cos_config, ...)``
# 覆盖，与既有 COS_* 常量同款；模块层不做额外缓存）。
# --------------------------------------------------------------------------- #
COS_FAST_TRANSPORT_DIRECT = "direct"
COS_FAST_TRANSPORT_FAST = "fast"
#: direct | fast；缺省/空 -> direct，其余非法值由调用方 fail-closed。
COS_DOWNLOAD_TRANSPORT = _env_str("COS_DOWNLOAD_TRANSPORT", "direct")
#: fast 转发基地址；必须 https（仅回环主机允许 http，供本机测试）。
COS_FAST_RELAY_URL = _env_str("COS_FAST_RELAY_URL")
#: fast 服务 token 私有文件（取首个非空行）；不打印内容。
COS_FAST_RELAY_TOKEN_FILE = _env_str("COS_FAST_RELAY_TOKEN_FILE")
#: 连接超时（秒）。
COS_FAST_RELAY_CONNECT_TIMEOUT = _env_int("COS_FAST_RELAY_CONNECT_TIMEOUT", 10)
#: 单次 socket 读空闲超时（秒）。
COS_FAST_RELAY_READ_TIMEOUT = _env_int("COS_FAST_RELAY_READ_TIMEOUT", 60)

# --------------------------------------------------------------------------- #
# capability（§8：off → internal → on；四项门禁未过前恒 off）
# --------------------------------------------------------------------------- #
#: off=任何身份不可用；internal=仅 owner 侧内部身份可见；on=全部可见。
#: 注意：即便 on，secret/bucket 缺失或 reconcile_required 时服务端仍判不可用。
COS_UPLOAD_CAPABILITY = _env_str("COS_UPLOAD_CAPABILITY", "off")

# --------------------------------------------------------------------------- #
# 本地直传通道（P0-W6a，2026-11-11）：无 COS 凭证的裸部署上传通道。
# 分片由浏览器直接 POST 平台（/api/ingestions/<job>/parts/<n>）写 staging，
# 复用 ingestion 状态机与 slide_publish 统一发布；配额闸沿用 upload_guard。
# --------------------------------------------------------------------------- #
#: 通道开关：on=可用（默认——本地裸部署开箱即传）；off=关闭（纯 COS 部署
#: 可显式关闭，capacity payload 随之 available=false）。
LOCAL_UPLOAD_CHANNEL = _env_str("LOCAL_UPLOAD_CHANNEL", "on")
#: 本地直传分片字节数（默认 8 MB；单请求体上限需 ≥ 此值）。
LOCAL_INGEST_PART_BYTES = _env_int("LOCAL_INGEST_PART_BYTES", 8_000_000)
#: 本地直传浏览器并发分片数（与 COS 通道同一起点）。
LOCAL_INGEST_PART_CONCURRENCY = _env_int("LOCAL_INGEST_PART_CONCURRENCY", 3)


def local_upload_enabled():
    """本地直传通道是否开启（env 词表判定；非 on 一律关闭，fail-closed）。"""
    return LOCAL_UPLOAD_CHANNEL == "on"

# --------------------------------------------------------------------------- #
# 桶与凭证（worker/签名共用；缺失即 blocked_external_input）
# --------------------------------------------------------------------------- #
COS_BUCKET = _env_str("COS_BUCKET")
COS_REGION = _env_str("COS_REGION")
COS_SECRET_ID_ENV = "COS_SECRET_ID"
COS_SECRET_KEY_ENV = "COS_SECRET_KEY"


def cos_credentials():
    """返回 (secret_id, secret_key, ok)。只在调用点取值，模块层不缓存明文。"""
    sid = (os.environ.get(COS_SECRET_ID_ENV) or "").strip()
    skey = (os.environ.get(COS_SECRET_KEY_ENV) or "").strip()
    return sid, skey, bool(sid and skey and COS_BUCKET and COS_REGION)


def cos_host():
    """virtual-hosted endpoint host：<bucket>.cos.<region>.myqcloud.com。"""
    return "%s.cos.%s.myqcloud.com" % (COS_BUCKET, COS_REGION)


def capability_available_for(role, has_credentials):
    """capability 可用性判定（服务端权威；前端只消费不下发秘密）。

    off → 恒 False；internal → 仅 owner；on → owner/user。凭证/桶配置缺失
    或对账暂停（reconcile_required）时调用方另行 fail-closed。
    """
    if COS_UPLOAD_CAPABILITY not in ("off", "internal", "on"):
        return False
    if not has_credentials:
        return False
    if COS_UPLOAD_CAPABILITY == "off":
        return False
    if COS_UPLOAD_CAPABILITY == "internal":
        return role == "owner"
    return role in ("owner", "user")
