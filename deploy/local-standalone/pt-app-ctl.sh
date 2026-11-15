#!/usr/bin/env bash
# PathTogether 本地部署 - 应用启动/停止/状态（WSL 内）
# 用法: bash pt-app-ctl.sh {start|stop|status|restart}
set -u
APP_DIR="$HOME/pt-app"
VENV="$HOME/pt-venv"
LOG_DIR="$HOME/pt-data/logs"
PID_FILE="$LOG_DIR/gunicorn.pid"
WORKER_PID_FILE="$LOG_DIR/cos-ingest-worker.pid"
PGBIN="$HOME/pt-venv/lib/python3.12/site-packages/pgserver/pginstall/bin"
PGDATA="$HOME/pt-pgdata"
PGPORT=5433
APP_PORT=8000

load_env() {
  export PORT=$APP_PORT
  export DATABASE_URL="postgresql://pt@127.0.0.1:$PGPORT/pathtogether"
  export UPLOAD_DIR="$HOME/pt-data/uploads"
  export SHARE_DATA_DIR="$HOME/pt-data/share"
  export PLUGIN_BUNDLES_DIR="$HOME/pt-data/plugins"
  export BOOTSTRAP_OWNER_LOGIN_ID="owner"
  export BOOTSTRAP_OWNER_PASSWORD_FILE="$HOME/pt-data/secrets/owner_password"
  export STORAGE_BACKEND="postgres"
  # P1-W1+（2026-11-15）：多通道伪彩面板（含每通道色环选色器）——荧光
  # 显色工作流刚需，本地部署默认开启
  export PATHTOGETHER_MULTICHANNEL_ENABLED=1
  # 上传解禁（2026-11-15）：单文件 50 GiB、每身份并行摄取 2、每小时 600 次。
  # 配额体系不变（owner/user01 总配额行已由 set_owner_storage_quota.py 调到
  # 1 TiB = admin API 硬边界）；磁盘 20 GiB 保留水位（UPLOAD_RESERVED_FREE_BYTES
  # 默认）仍生效，是物理保护。env 为应用 import 期读取，改动须 restart 生效。
  export UPLOAD_MAX_REQUEST_BYTES=53687091200      # 50 GiB（默认 10 GiB）
  export COS_MAX_ACTIVE_UPLOADS_PER_IDENTITY=2     # 默认 1（每身份串行导入）
  export COS_MAX_ACTIVE_UPLOADS_GLOBAL=4           # 默认 2
  export UPLOAD_HOURLY_REQUEST_LIMIT=600           # 默认 60
  # 注意：ingestion_jobs_one_waiting_per_owner DB 唯一索引（0066）不受 env
  # 影响——同一时刻至多 活跃2+等待1=3 个任务在系统内，第 4 个创建请求 409
  # cos_waiting_limit；大批量导入分批进行（每批 ≤3 个连续创建）。
  # HISTOPILOT_URL 不设置 → AI 对话功能优雅降级（不影响阅片/标注/插件）
}

wait_healthy() {
  for i in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:$APP_PORT/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

case "${1:-status}" in
  start)
    # 1) PG
    "$PGBIN/pg_isready" -p $PGPORT -h 127.0.0.1 >/dev/null 2>&1 || \
      "$PGBIN/pg_ctl" -D "$PGDATA" -o "-p $PGPORT -c listen_addresses=127.0.0.1" -w start >/dev/null
    # 2) 首启密码文件（已存在则不覆盖）
    if [ ! -s "$HOME/pt-data/secrets/owner_password" ]; then
      umask 077
      python3 -c "import secrets; print(secrets.token_urlsafe(12))" > "$HOME/pt-data/secrets/owner_password"
      echo "[首次部署] owner 初始密码已生成: $HOME/pt-data/secrets/owner_password"
    fi
    # 3) gunicorn（已在跑则跳过）
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "app already running (pid $(cat "$PID_FILE"))"
    else
      cd "$APP_DIR"
      load_env
      "$VENV/bin/gunicorn" "share_server:combined_app" \
        -w 2 --threads 8 \
        -b "0.0.0.0:$APP_PORT" \
        --pid "$PID_FILE" \
        --access-logfile "$LOG_DIR/access.log" \
        --error-logfile "$LOG_DIR/error.log" \
        --daemon
      echo "gunicorn starting on :$APP_PORT ..."
    fi
    # 3b) 摄取 worker（P0-W6a：本地直传通道的校验/发布/readiness 排水；
    #     cos_ingest_worker --loop 对 local_http 任务无 COS 凭证依赖）
    if [ -f "$WORKER_PID_FILE" ] && kill -0 "$(cat "$WORKER_PID_FILE")" 2>/dev/null; then
      echo "ingest worker already running (pid $(cat "$WORKER_PID_FILE"))"
    else
      cd "$APP_DIR"
      load_env
      nohup bash -c 'while :; do "$0" "$2/cos_ingest_worker.py" --loop >> "$1/ingest-worker.log" 2>&1 || true; sleep 2; done' \
        "$VENV/bin/python" "$LOG_DIR" "$APP_DIR" >/dev/null 2>&1 &
      echo $! > "$WORKER_PID_FILE"
      echo "ingest worker starting (pid $(cat "$WORKER_PID_FILE"))"
    fi
    # 4) 健康检查
    if wait_healthy; then
      echo "HEALTHY http://127.0.0.1:$APP_PORT  (owner / $(cat "$HOME/pt-data/secrets/owner_password" 2>/dev/null))"
    else
      echo "FAILED to become healthy; check $LOG_DIR/error.log"; exit 1
    fi
    ;;
  stop)
    [ -f "$PID_FILE" ] && kill "$(cat "$PID_FILE")" 2>/dev/null && echo "app stopped" || echo "app not running"
    if [ -f "$WORKER_PID_FILE" ] && kill -0 "$(cat "$WORKER_PID_FILE")" 2>/dev/null; then
      # 杀掉 worker 包装 shell 及其 python 子进程
      WPID="$(cat "$WORKER_PID_FILE")"
      pkill -P "$WPID" 2>/dev/null || true
      kill "$WPID" 2>/dev/null && echo "ingest worker stopped" || true
    fi
    rm -f "$WORKER_PID_FILE"
    "$PGBIN/pg_ctl" -D "$PGDATA" -w stop >/dev/null 2>&1 || true
    echo "pg stopped"
    ;;
  status)
    "$PGBIN/pg_isready" -p $PGPORT -h 127.0.0.1 >/dev/null 2>&1 && echo "pg: running" || echo "pg: stopped"
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "app: running (pid $(cat "$PID_FILE")) :$APP_PORT"
    else
      echo "app: stopped"
    fi
    if [ -f "$WORKER_PID_FILE" ] && kill -0 "$(cat "$WORKER_PID_FILE")" 2>/dev/null; then
      echo "ingest worker: running (pid $(cat "$WORKER_PID_FILE"))"
    else
      echo "ingest worker: stopped"
    fi
    ;;
  restart)
    "$0" stop; sleep 1; "$0" start
    ;;
esac
