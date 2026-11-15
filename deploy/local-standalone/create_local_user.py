#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PathTogether 本地部署 —— 主机侧建号脚本（方案 B）。

背景：平台自助注册为 fail-closed 设计（public 模式前置 HTTPS 入口 +
Secure Cookie + 生产级 SMTP 邮箱验证 + 双协议文稿发布，任一缺失自动降级
closed）；管理台手动建号端点已退役（410 endpoint_retired），官方 useradmin
CLI 仅支持重置 owner 密码。本地 HTTP 部署因此没有任何 Web 侧建号入口。

本脚本作为主机侧运维入口，**复用平台自身的**
``user_store_pg.create_user_with_total_allowance``（同源 werkzeug pbkdf2
哈希；单事务内建用户 + 总额度行 + credits 钱包 + user.create 审计），
不绕过任何平台不变量；用户即建即 active（activation_source='admin'）。

用法（WSL 内执行；缺省连本地部署 PG，可用 DATABASE_URL 覆盖）::

    ~/pt-venv/bin/python /mnt/f/切片三维重建软件平台构建/本地部署/create_local_user.py \
        <login_id> [--display-name 名字] [--no-ai-access] \
        [--limit-nano-cny N] [--password-stdin | --password-file PATH]

    # login_id 规范化：去首尾空白 + 小写（与平台一致）
    # 密码策略 15..200 字符（与 user_store_pg 同源常量）
    # 缺省交互输入密码（两次确认，无回显）；绝不接受命令行明文密码
    # 缺省额度：ai_spend_total_defaults 全局默认（当前 ¥20）

退出码：0 成功；1 操作拒绝/失败；2 参数用法错误（argparse 默认）。
"""

import argparse
import getpass
import os
import sys

# ---- 应用代码定位（必须在 import 应用模块前就位）----
APP_DIR = (os.environ.get("PT_APP_DIR") or os.path.expanduser("~/pt-app")).strip()
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# 缺省连接本地部署内嵌 PG（与 pt-app-ctl.sh load_env 同值）
os.environ.setdefault(
    "DATABASE_URL", "postgresql://pt@127.0.0.1:5433/pathtogether")

import psycopg  # noqa: E402

import pg_store  # noqa: E402
from user_store_pg import (  # noqa: E402
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    create_user_with_total_allowance,
)


def _err(msg: str) -> None:
    """错误一律走 stderr（成功信息才进 stdout）。"""
    sys.stderr.write("create_local_user: 错误：%s\n" % msg)


def _owner_actor_id():
    """取唯一启用 owner 的 user_id 作为审计 actor；缺失返回 None。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM users "
                "WHERE role='owner' AND NOT disabled LIMIT 1")
            row = cur.fetchone()
            return row["user_id"] if row else None
    finally:
        conn.close()


def _read_password(args) -> str:
    """密码来源：--password-stdin / --password-file / 交互两次确认。"""
    if args.password_stdin:
        data = sys.stdin.read()
    elif args.password_file:
        with open(args.password_file, "r", encoding="utf-8") as fh:
            data = fh.read()
    else:
        first = getpass.getpass("新账号密码（%d..%d 字符）: "
                                % (PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH))
        second = getpass.getpass("再次输入确认: ")
        if first != second:
            raise ValueError("两次输入不一致")
        return first
    # 文件/stdin 只去单个尾部换行（与 useradmin 口径一致，其余空白保留）
    for nl in ("\r\n", "\n\r"):
        if data.endswith(nl):
            return data[:-2]
    return data[:-1] if data.endswith(("\n", "\r")) else data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="create_local_user.py",
        description="PathTogether 本地部署主机侧建号（复用平台同源创建路径）")
    parser.add_argument("login_id", help="登录账号（规范化：strip + 小写）")
    parser.add_argument("--display-name", default=None,
                        help="显示名（缺省 = 登录账号）")
    parser.add_argument("--no-ai-access", action="store_true",
                        help="建号即关闭 AI 访问（缺省开启）")
    parser.add_argument("--limit-nano-cny", type=int, default=None,
                        help="总额度（nano-CNY 整数；缺省用平台全局默认）")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--password-stdin", action="store_true",
                     help="从 stdin 读密码（管道/脚本调用）")
    src.add_argument("--password-file", metavar="PATH",
                     help="从文件读密码（去单个尾部换行）")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    login_id = str(args.login_id or "").strip().lower()
    if not login_id:
        _err("登录账号不能为空")
        return 1
    try:
        password = _read_password(args)
    except ValueError as exc:
        _err(str(exc))
        return 1
    if not (PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH):
        _err("密码长度须在 %d..%d 字符之间（收到 %d 字符）"
             % (PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH, len(password)))
        return 1

    try:
        actor = _owner_actor_id()
        user, allowance = create_user_with_total_allowance(
            login_id,
            password,
            display_name=args.display_name,
            ai_access=not args.no_ai_access,
            total_limit_nano_cny=args.limit_nano_cny,
            actor_user_id=actor,
        )
    except ValueError as exc:
        _err(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - 统一映射，不泄露口令
        _err("建号失败：%s（数据库不可达请先启动平台："
             "本地部署/PathTogether一键启动.bat）" % exc)
        return 1

    limit_cny = int(allowance["limit_nano_cny"]) / 1e9
    print("建号成功")
    print("  user_id      : %s" % user["user_id"])
    print("  login_id     : %s" % user["login_id"])
    print("  display_name : %s" % (user.get("display_name") or user["login_id"]))
    print("  ai_access    : %s" % bool(not args.no_ai_access))
    print("  总额度       : ¥%.2f（%d nano-CNY）"
          % (limit_cny, int(allowance["limit_nano_cny"])))
    print("  状态         : active（可直接登录 http://localhost:8000/login）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
