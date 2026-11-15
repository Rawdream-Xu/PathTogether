#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PathTogether 本地部署 —— 主机侧存储配额调整脚本（方案 B，仿 create_local_user.py）。

背景（2026-11-15 上传解禁）：owner parity（2026-11-06）由
credit_migration._ensure_owner_quota_row_tx 幂等建行，但对**已存在**的
配额行 ON CONFLICT DO NOTHING——本地部署的 owner / user01 两行建于该
升级之前，仍是最早的 10 GiB 默认值，user01 已用 8.2 GiB 即撞墙。本脚本
把指定账号的存储配额调到目标值（缺省 1 TiB = 平台 admin API 硬边界
``upload_guard.UPLOAD_USER_QUOTA_MAX_BYTES``）。

本脚本**复用平台自身的** ``upload_guard.set_user_quota``（行锁 + 同事务
审计 ``upload.user_quota_update``），不绕过任何平台不变量；只改
quota_bytes，used/reserved/extra 不动。

用法（WSL 内执行）::

    ~/pt-venv/bin/python /mnt/f/切片三维重建软件平台构建/本地部署/set_owner_storage_quota.py \
        <login_id> [更多login_id...] [--quota-gib 1024 | --quota-bytes N]

退出码：0 全部成功；1 有失败；2 参数用法错误。
"""

import argparse
import os
import sys

# ---- 应用代码定位（必须在 import 应用模块前就位）----
APP_DIR = (os.environ.get("PT_APP_DIR") or os.path.expanduser("~/pt-app")).strip()
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# 缺省连接本地部署内嵌 PG（与 pt-app-ctl.sh load_env 同值）
os.environ.setdefault(
    "DATABASE_URL", "postgresql://pt@127.0.0.1:5433/pathtogether")
os.environ.setdefault("STORAGE_BACKEND", "postgres")

import psycopg  # noqa: E402

import pg_store  # noqa: E402
import upload_guard  # noqa: E402


def _err(msg):
    sys.stderr.write("set_owner_storage_quota: 错误：%s\n" % msg)


def _owner_actor_id(conn):
    """取唯一启用 owner 的 user_id 作审计 actor（与 create_local_user 同口径）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id FROM users "
            "WHERE role='owner' AND NOT disabled LIMIT 1")
        row = cur.fetchone()
    if row is None:
        raise SystemExit("set_owner_storage_quota: 错误：库内无启用 owner 账号")
    return row["user_id"]


def _resolve_user(conn, login_id):
    """login_id（strip+小写规范化）→ (canonical, {user_id, role})；
    不存在/停用返回 (canonical, None)。"""
    canonical = login_id.strip().lower()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, role FROM users "
            "WHERE login_id = %s AND NOT disabled LIMIT 1", (canonical,))
        row = cur.fetchone()
    return canonical, (dict(row) if row else None)


def _current_quota(conn, user_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT quota_bytes FROM upload_user_quotas WHERE user_id = %s",
            (user_id,))
        row = cur.fetchone()
    return row["quota_bytes"] if row else None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="调整本地部署用户存储配额（复用 upload_guard.set_user_quota）")
    ap.add_argument("login_ids", nargs="+", help="目标账号 login_id（可多个）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--quota-gib", type=int, default=1024,
                   help="目标配额（GiB，缺省 1024 = 1 TiB 硬边界）")
    g.add_argument("--quota-bytes", type=int,
                   help="目标配额（字节，优先于 --quota-gib）")
    args = ap.parse_args(argv)

    quota = (args.quota_bytes if args.quota_bytes is not None
             else args.quota_gib * 1024 ** 3)
    try:
        upload_guard._validate_quota_bytes(quota)
    except ValueError as exc:
        _err(str(exc))
        return 2

    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    try:
        actor = _owner_actor_id(conn)
        failures = 0
        for login_id in args.login_ids:
            canonical, user = _resolve_user(conn, login_id)
            if user is None:
                _err("%s：用户不存在或已停用" % canonical)
                failures += 1
                continue
            user_id, role = user["user_id"], user["role"]
            before = _current_quota(conn, user_id)
            upload_guard.set_user_quota(
                user_id, quota, actor_user_id=actor, updated_by=actor)
            after = _current_quota(conn, user_id)
            print("%s (%s, %s): quota_bytes %s -> %s (%.1f GiB)"
                  % (canonical, user_id, role,
                     "无行" if before is None else before,
                     after, (after or 0) / 1024 ** 3))
        if failures:
            _err("%d 个账号失败" % failures)
            return 1
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
