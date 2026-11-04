#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Credit 钱包迁移 CLI（docs/credit-wallet-implementation-spec-20261027.md
§10；方案 §9.2/§9.3）。

子命令：

    python scripts/migrate_credit_wallets.py plan
    python scripts/migrate_credit_wallets.py apply --expected-hash H --actor usr_owner_x
    python scripts/migrate_credit_wallets.py verify
    python scripts/migrate_credit_wallets.py set-new-user-backend --value credits --actor usr_owner_x

纪律：

- 数据库连接只从环境变量读（``DATABASE_URL`` 或 PGHOST/PGPORT/PGUSER/
  PGPASSWORD/PGDATABASE），**绝不接受 --dsn**；
- 输出 JSON，只含 user_id/账户/金额标量，不含密码、邮箱、密钥或完整支付
  参数；
- ``apply`` 要求显式 ``--actor`` 与 ``--expected-hash``；维护闸未开或 hash
  不符时脚本非零退出且不写任何行；
- 本脚本**永不自动修数据**：plan/verify 只读，apply 只做方案 §9.1 的期初
  迁移，异常留给人工核对。
"""

import argparse
import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_SCRIPT_DIR))

import pg_store  # noqa: E402

import credit_migration  # noqa: E402


def _die(message, code=2):
    print("migrate_credit_wallets: %s" % message, file=sys.stderr)
    raise SystemExit(code)


def _require_conninfo():
    try:
        pg_store.get_conninfo()
    except RuntimeError as exc:
        _die(str(exc), 2)


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_plan(_args):
    _print(credit_migration.plan())
    return 0


def cmd_apply(args):
    report = credit_migration.apply(
        args.expected_hash, actor_user_id=args.actor)
    _print(report)
    if report["aborted"]:
        _die("apply 中止：%s（未写任何行）" % report["aborted"], 1)
    return 0 if report["ok"] else 1


def cmd_verify(_args):
    report = credit_migration.verify()
    _print(report)
    return 0 if report["ok"] else 1


def cmd_set_new_user_backend(args):
    _print(credit_migration.set_new_user_backend(
        args.value, actor_user_id=args.actor))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Credit 钱包迁移（plan/apply/verify/set-new-user-backend）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="只读盘点迁移计划与 hash")
    p_plan.set_defaults(func=cmd_plan)

    p_apply = sub.add_parser("apply", help="按已确认 hash 执行迁移")
    p_apply.add_argument("--expected-hash", required=True,
                         help="plan 输出的 plan_hash（必须完全一致）")
    p_apply.add_argument("--actor", required=True,
                         help="执行迁移的 owner user_id（审计用）")
    p_apply.set_defaults(func=cmd_apply)

    p_verify = sub.add_parser("verify", help="迁移后只读复核 + 对账摘要")
    p_verify.set_defaults(func=cmd_verify)

    p_backend = sub.add_parser(
        "set-new-user-backend", help="切换新用户后端（legacy|credits）")
    p_backend.add_argument("--value", required=True,
                           choices=credit_migration.NEW_USER_BACKENDS)
    p_backend.add_argument("--actor", required=True,
                           help="执行切换的 owner user_id（审计用）")
    p_backend.set_defaults(func=cmd_set_new_user_backend)

    args = parser.parse_args(argv)
    _require_conninfo()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
