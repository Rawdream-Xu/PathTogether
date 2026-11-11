#!/bin/sh
# PathTogether platform entrypoint. AI/navigation services are separate plugins
# and are never started or health-gated by this container.
set -eu

# ---------------------------------------------------------------------------
# 账户引导 footgun 守护（账户系统批次 A，docs §5.1/§10）：
#   - 「REQUIRE_ADMIN_AUTH=1 必须有 bootstrap 秘密」的硬性拒启已移除——
#     数据库已有 owner 时无需 bootstrap 秘密即可启动；空库/无 owner 等状态
#     由应用启动状态机 fail-fast（错误消息更可读，见 app.py §5.2）；
#   - BOOTSTRAP_OWNER_PASSWORD_FILE 被设置但文件不存在/为空/内容为占位符
#     → 拒启（复制 admin.env 未替换）；存在且非占位符则原样传给应用
#     （应用读文件内容，entry 不展开进环境）。
#   - 一版兼容别名 ADMIN_USERNAME / ADMIN_PASSWORD 已随 R3 Wave2-Compat
#     删除：entry 不再检查旧变量，残留配置被直接忽略。
# ---------------------------------------------------------------------------
_BOOTSTRAP_PASSWORD_SENTINEL="<REPLACE_WITH_STRONG_PASSWORD>"
_boot_pw_file="${BOOTSTRAP_OWNER_PASSWORD_FILE:-}"
if [ -n "$_boot_pw_file" ]; then
  if [ ! -f "$_boot_pw_file" ]; then
    echo "[entry] BOOTSTRAP_OWNER_PASSWORD_FILE=$_boot_pw_file does not exist; refusing to start" >&2
    exit 1
  fi
  if [ ! -s "$_boot_pw_file" ]; then
    echo "[entry] BOOTSTRAP_OWNER_PASSWORD_FILE=$_boot_pw_file is empty; refusing to start" >&2
    exit 1
  fi
  # 占位符内容（精确 sentinel / <...> 包裹）= 复制 admin.env 未替换：
  # 与应用层 _is_placeholder_admin_password 同口径，提前拒启（fail-fast）。
  # 命令替换已去除尾部换行；真密码不含 <...> 包裹形态。
  _boot_pw="$(cat "$_boot_pw_file")"
  case "$_boot_pw" in
    "$_BOOTSTRAP_PASSWORD_SENTINEL"|\<*\>)
      echo "[entry] BOOTSTRAP_OWNER_PASSWORD_FILE=$_boot_pw_file contains a placeholder; refusing to start" >&2
      exit 1
      ;;
  esac
fi

# PostgreSQL 为唯一后端；启动时 apply schema（幂等）。
python3 -c '
import pg_store
conn = pg_store.connect()
try:
    pg_store.ensure_schema(conn)
finally:
    conn.close()
'

mkdir -p "${UPLOAD_DIR:-/data/uploads}" "${SHARE_DATA_DIR:-/data/share}" \
  "${PLUGIN_BUNDLES_DIR:-/data/plugins}" \
  "${FORMAT_REQUEST_DIR:-/data/format-requests}"

# ---------------------------------------------------------------------------
# sample-tma-score 后端（demo 同容器托管）
#
# 监听 127.0.0.1:8061（与 manifest.service.baseUrl 一致），随本容器起停，
# 崩溃 2s 后自动拉起。消掉「重建后忘记 podman exec」的运维点。
# SAMPLE_TMA_BACKEND=0/false/no/off 关闭。生产形态仍应独立容器（P2）；
# 此处只保证 demo 镜像内示例能力在容器生命周期内可用。
# gunicorn 仍 exec 为 PID 1；本后台循环被 reparent 到 PID 1，容器 cgroup
# 回收时一并杀掉。子进程退出由循环自身 wait，不会被 gunicorn waitpid(-1)
# 误当成 worker。
# ---------------------------------------------------------------------------
_sample_tma="$(printf '%s' "${SAMPLE_TMA_BACKEND:-1}" | tr '[:upper:]' '[:lower:]')"
case "$_sample_tma" in
  0|false|no|off)
    echo "[entry] SAMPLE_TMA_BACKEND=$_sample_tma, skip sample-tma-score backend"
    ;;
  *)
    _tma_py="/app/plugins/sample-tma-score/backend/app.py"
    if [ -f "$_tma_py" ]; then
      echo "[entry] starting sample-tma-score backend on 127.0.0.1:${PT_TMA_SCORE_PORT:-8061}"
      (
        while :; do
          python3 "$_tma_py" || true
          echo "[entry] sample-tma-score backend exited, restart in 2s" >&2
          sleep 2
        done
      ) &
    else
      echo "[entry] sample-tma-score backend not in image, skip"
    fi
    ;;
esac

# ---------------------------------------------------------------------------
# 注册验证邮件 worker（I 线）：registration_mail_jobs 权威排水。
# REGISTRATION_MAIL_WORKER=0/false/no/off 关闭；未配置发送通道时 drain 保
# queued、不拒启。与 tma 后端同款：后台循环崩溃 2s 重启，gunicorn 仍 PID 1。
# ---------------------------------------------------------------------------
_mail_worker="$(printf '%s' "${REGISTRATION_MAIL_WORKER:-1}" | tr '[:upper:]' '[:lower:]')"
case "$_mail_worker" in
  0|false|no|off)
    echo "[entry] REGISTRATION_MAIL_WORKER=$_mail_worker, skip mail worker"
    ;;
  *)
    echo "[entry] starting registration_mail_worker --loop"
    (
      while :; do
        python3 /app/registration_mail_worker.py --loop || true
        echo "[entry] registration_mail_worker exited, restart in 2s" >&2
        sleep 2
      done
    ) &
    ;;
esac

# ---------------------------------------------------------------------------
# 格式兼容请求邮件 worker：PostgreSQL format_request_mail_jobs 权威排水。
# FORMAT_REQUEST_WORKER=0/false/no/off 关闭。发送通道与注册邮件共用
# REGISTRATION_MAIL_SENDER（homePC 已配 smtp）；未配置时 drain 保 queued。
# ---------------------------------------------------------------------------
_fr_worker="$(printf '%s' "${FORMAT_REQUEST_WORKER:-1}" | tr '[:upper:]' '[:lower:]')"
case "$_fr_worker" in
  0|false|no|off)
    echo "[entry] FORMAT_REQUEST_WORKER=$_fr_worker, skip format-request worker"
    ;;
  *)
    echo "[entry] starting format_request_worker --loop"
    (
      while :; do
        python3 /app/scripts/format_request_worker.py --loop || true
        echo "[entry] format_request_worker exited, restart in 2s" >&2
        sleep 2
      done
    ) &
    ;;
esac

# ---------------------------------------------------------------------------
# 研究副本删除 worker（docs/agent-plan-20260921 §6.3/§8）：research_data_
# deletion_jobs 权威排水 + 研究副本 90 天到期清理。RESEARCH_DELETION_
# WORKER=0/false/no/off 关闭。多实例/重启安全（FOR UPDATE SKIP LOCKED +
# 幂等删除 + running 租约回收），与 gunicorn 独立、后台循环崩溃 2s 重启。
# ---------------------------------------------------------------------------
_rd_worker="$(printf '%s' "${RESEARCH_DELETION_WORKER:-1}" | tr '[:upper:]' '[:lower:]')"
case "$_rd_worker" in
  0|false|no|off)
    echo "[entry] RESEARCH_DELETION_WORKER=$_rd_worker, skip research deletion worker"
    ;;
  *)
    echo "[entry] starting research_deletion_worker --loop"
    (
      while :; do
        python3 /app/research_deletion_worker.py --loop || true
        echo "[entry] research_deletion_worker exited, restart in 2s" >&2
        sleep 2
      done
    ) &
    ;;
esac

# ---------------------------------------------------------------------------
# KFB 转换 worker（Phase B）：conversion_jobs 排水。独立于 Gunicorn。
# R1 起服务端不再新建转换（KFB/KFBF 在浏览器本机转换后上传），缺省关闭；
# 旧镜像建的任务只能在旧镜像跑完（新 worker 对无 slide_id 旧任务 fail-closed）。
# CONVERSION_WORKER=1/true/yes/on 才拉起。
# ---------------------------------------------------------------------------
_cv_worker="$(printf '%s' "${CONVERSION_WORKER:-0}" | tr '[:upper:]' '[:lower:]')"
case "$_cv_worker" in
  1|true|yes|on)
    echo "[entry] starting conversion_worker --loop"
    (
      while :; do
        python3 /app/conversion_worker.py --loop || true
        echo "[entry] conversion_worker exited, restart in 2s" >&2
        sleep 2
      done
    ) &
    ;;
  *)
    echo "[entry] CONVERSION_WORKER=$_cv_worker, skip conversion worker"
    ;;
esac

# ---------------------------------------------------------------------------
# COS 直传摄取 worker（Phase 2，docs/cos-direct-upload-audit-plan.md §10）：
# ingestion_jobs 排水（核对/合并/版本化下载/本地入库/readiness/清理/对账）。
# 默认关闭：COS capability 未上线（§8 off），避免生产无谓空转噪声；
# COS 部署时随 env 开启（COS_INGEST_WORKER=1 且配置 bucket/region/secret）。
# P0-W6a（2026-11-11）：本地直传通道（LOCAL_UPLOAD_CHANNEL，默认 on）也
# 由同一 worker 排水（local_http 任务在 downloading 段就地转 validating，
# 无 COS 凭证依赖）——通道开启即拉起，空转成本为每轮一次空 claim 查询。
# ---------------------------------------------------------------------------
_local_channel="$(printf '%s' "${LOCAL_UPLOAD_CHANNEL:-on}" | tr '[:upper:]' '[:lower:]')"
_cos_worker="$(printf '%s' "${COS_INGEST_WORKER:-0}" | tr '[:upper:]' '[:lower:]')"
case "$_cos_worker" in
  1|true|yes|on) ;;
  *)
    case "$_local_channel" in
      1|true|yes|on) _cos_worker=1 ;;
    esac
    ;;
esac
case "$_cos_worker" in
  1|true|yes|on)
    echo "[entry] starting cos_ingest_worker --loop"
    (
      while :; do
        python3 /app/cos_ingest_worker.py --loop || true
        echo "[entry] cos_ingest_worker exited, restart in 2s" >&2
        sleep 2
      done
    ) &
    ;;
  *)
    echo "[entry] COS_INGEST_WORKER=$_cos_worker, skip cos-ingest worker"
    ;;
esac

# ---------------------------------------------------------------------------
# 百度分享导入 worker：枚举 + 转存/下载批次。默认关闭（真实外部动作
# 需显式 BAIDU_ENUMERATION_ENABLED / BAIDU_IMPORT_ENABLED）。
# BAIDU_IMPORT_WORKER=1/true 才拉起；缺省 skip。
# ---------------------------------------------------------------------------
_bd_worker="$(printf '%s' "${BAIDU_IMPORT_WORKER:-0}" | tr '[:upper:]' '[:lower:]')"
case "$_bd_worker" in
  1|true|yes|on)
    echo "[entry] starting baidu_import_worker --loop"
    (
      while :; do
        python3 /app/scripts/baidu_import_worker.py --loop || true
        echo "[entry] baidu_import_worker exited, restart in 2s" >&2
        sleep 2
      done
    ) &
    ;;
  *)
    echo "[entry] BAIDU_IMPORT_WORKER=$_bd_worker, skip baidu-import worker"
    ;;
esac

# Serve public /s/* links and the authenticated workbench on the same origin.
# The combined application keeps share-token checks separate from account auth.
exec gunicorn share_server:combined_app \
  -b "0.0.0.0:${PORT:-8000}" \
  -w "${GUNICORN_WORKERS:-2}" \
  --threads "${GUNICORN_THREADS:-8}"
