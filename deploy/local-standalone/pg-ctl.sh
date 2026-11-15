#!/usr/bin/env bash
# PathTogether 本地部署 - PostgreSQL 初始化/启动（WSL 内，无需 sudo）
# 用法: bash pg-ctl.sh {init|start|stop|status}
set -u
PGBIN="$HOME/pt-venv/lib/python3.12/site-packages/pgserver/pginstall/bin"
PGDATA="$HOME/pt-pgdata"
PGPORT=5433
OPTS="-p $PGPORT -c listen_addresses=127.0.0.1"

case "${1:-status}" in
  init)
    if [ -f "$PGDATA/PG_VERSION" ]; then echo "PG data already initialized at $PGDATA"; exit 0; fi
    mkdir -p "$HOME/pt-data/uploads" "$HOME/pt-data/share" "$HOME/pt-data/plugins" "$HOME/pt-data/secrets" "$HOME/pt-data/logs"
    "$PGBIN/initdb" -D "$PGDATA" --auth=trust --encoding=utf8 --locale=C -U pt
    "$PGBIN/pg_ctl" -D "$PGDATA" -o "$OPTS" -w start
    "$PGBIN/createdb" -p $PGPORT -U pt pathtogether
    echo "PG ready on 127.0.0.1:$PGPORT (user=pt db=pathtogether, trust auth)"
    ;;
  start)
    "$PGBIN/pg_ctl" -D "$PGDATA" -o "$OPTS" -w start 2>&1 || true
    "$PGBIN/pg_isready" -p $PGPORT -h 127.0.0.1
    ;;
  stop)
    "$PGBIN/pg_ctl" -D "$PGDATA" -w stop
    ;;
  status)
    "$PGBIN/pg_isready" -p $PGPORT -h 127.0.0.1 && echo "PG running" || echo "PG not running"
    ;;
esac
