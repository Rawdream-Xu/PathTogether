# -*- coding: utf-8 -*-
"""Credit 钱包迁移与新用户赠送（docs/credit-wallet-implementation-spec-
20261027.md §10；方案 docs/credit-recharge-implementation-plan-20261026.md
§9）。

本模块包含两件事：

1. **新用户赠送**：``credit_new_user_backend`` 为 ``credits`` 时，三条建号
   路径（owner 建号 / 公开注册 / 登录惰性激活）在**同一事务**内、
   ``create_user_total_allowance_tx`` 之后调用
   :func:`provision_signup_wallet_tx`——把默认总额度按 1:1 转为
   ``promotional`` 批次（``source_key='signup:<user_id>'``）。旧总额度行
   照常创建（spec §10），只是随后被 0088 触发器冻结为历史。
2. **旧余额迁移**：``plan`` 只读盘点 → ``apply`` 在维护闸内逐用户独立
   事务把 ``legacy_grant_units = max(0, limit − spent)`` 写成不可退款的
   ``legacy_grant`` 批次并切换账户到 ``credits`` → ``verify`` 复核 →
   ``set_new_user_backend`` 收尾。脚本 ``scripts/migrate_credit_wallets.py``
   只是本模块的薄 CLI。

迁移口径（方案 §9.1）：

- ``legacy_grant_units = max(0, old_limit_nano_cny − old_spent_nano_cny)``，
  1:1 使用 ``credit_store.UNITS_PER_CNY``（数值上 nano-CNY = credit_units）；
- 只迁移 role=user（含禁用）；owner/demo 保持旧预算路径；
- ``reserved != 0`` 或存在 open legacy hold（**按 subject_type/subject_id
  统计，不能只按 account_id**——旧 hold 的 account_id 可能为空）→ 跳过；
- 零剩余只写账户切换与迁移快照，不发行零额批次；
- 每用户独立事务，单用户失败只回滚该用户；同一 ``source_key``
  （``legacy:<user_id>:v1``）保证重跑不重复发放；
- ``apply`` 要求 ``ai_dispatch_maintenance=true``（运维已排空在途调用）、
  持与建号/兑换共用的会话级 advisory lock，并要求重算的 plan hash 与
  ``--expected-hash`` 完全一致；任一不满足即整体中止、不写任何行。

本模块永不修改旧 ledger 金额/条数，也永不自动修数据。
"""

import hashlib
import json

import psycopg

import credit_store
import pg_store
import settings_store
import share_store_pg
import spend_store

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: platform_settings 键：新用户走哪个后端（JSON 字符串 legacy|credits）
NEW_USER_BACKEND_KEY = "credit_new_user_backend"
#: 合法取值；缺键/非法值按 legacy（spec §2）
NEW_USER_BACKENDS = ("legacy", "credits")
#: 新用户赠送批次来源键前缀
SIGNUP_SOURCE_KEY_PREFIX = "signup:"
#: 迁移批次来源键模板（同一用户只发行一次）
LEGACY_SOURCE_KEY_TEMPLATE = "legacy:%s:v1"
#: 迁移快照版本（写入 credit_migration.source_version）
MIGRATION_SOURCE_VERSION = "v1"
#: 迁移审计动作（detail 只含 user_id/grant_units/plan_hash）
MIGRATION_AUDIT_ACTION = "credit.wallet_migrated"
#: 新用户后端切换审计动作
NEW_USER_BACKEND_AUDIT_ACTION = "credit.new_user_backend_changed"

# --------------------------------------------------------------------------- #
# owner 迁移（2026-11-06 owner parity；docs/owner-parity-plan-20261106.md §A）
# --------------------------------------------------------------------------- #
#: owner 迁移赠送（Credits；promotional 批次，不按月重置）
OWNER_GRANT_CREDITS = 100_000
#: owner 赠送批次来源键模板（同一 owner 只发行一次；幂等重跑返回原批次）
OWNER_SOURCE_KEY_TEMPLATE = "owner-promotional:%s:v1"
#: owner 迁移快照标识（写入 credit_migration.source）
OWNER_MIGRATION_SOURCE = "owner_parity_v1"
#: owner 迁移审计动作
OWNER_MIGRATION_AUDIT_ACTION = "credit.owner_migrated"
#: owner 基础存储容量（1 TiB；行不存在时建行，不改 env 默认 10 GiB）
OWNER_BASE_QUOTA_BYTES = 1024 ** 4
#: 计入 owner 已用量（used_bytes）的切片状态过滤：迁移口径与普通用户一致，
#: 排除已删除 / 删除中 / 暂存（staging）资产；`accounted_bytes` 为 NULL 的
#: 无账历史切片计入 `unaccounted_slides`，但不贡献字节。
OWNER_USAGE_EXCLUDED_ASSET_STATES = ("deleted", "deleting", "staging")
#: owner 迁移赠送原因文案（流水 reason）
OWNER_GRANT_REASON = "owner parity migration grant"
#: owner plan 状态词表（顺序即报告顺序）
OWNER_PLAN_STATUSES = (
    "ready",
    "already_migrated",
    "grant_mismatch",
    "not_owner",
)

#: plan 状态词表（顺序即报告顺序）
PLAN_STATUSES = (
    "ready",
    "already_credits",
    "skip_reserved",
    "skip_open_holds",
    "skip_no_allowance",
)

#: 迁移原因（批次/审计统一文案）
MIGRATION_REASON = "legacy allowance migration"
SIGNUP_REASON = "signup grant"


class CreditMigrationError(Exception):
    """迁移参数/状态错误（CLI 打印后非零退出；不写任何行）。"""

    code = "credit_migration_error"


# --------------------------------------------------------------------------- #
# 连接与通用工具
# --------------------------------------------------------------------------- #
def _connect():
    """建连接并设 dict_row（本模块所有查询按列名访问）。"""
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _is_int(value):
    """bool 不是金额整数。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise CreditMigrationError("%s 需为非空字符串" % field)
    return value.strip()


def _require_nano(value, field):
    if not _is_int(value) or value < 0:
        raise CreditMigrationError("%s 需为非负整数 nano-CNY" % field)
    return value


def _format_units(units, scale):
    """整数 units → 精确十进制字符串（去尾零；不经过 float）。"""
    units = int(units)
    sign = "-" if units < 0 else ""
    units = abs(units)
    whole, fraction = divmod(units, scale)
    if fraction == 0:
        return "%s%d" % (sign, whole)
    digits = str(fraction).rjust(len(str(scale - 1)), "0").rstrip("0")
    return "%s%d.%s" % (sign, whole, digits)


def _units_payload(units, prefix="grant"):
    """units 的三种展示形态（``<prefix>_units/_credits/_cny``）。"""
    return {
        prefix + "_units": int(units),
        prefix + "_credits": _format_units(
            units, credit_store.UNITS_PER_CREDIT),
        prefix + "_cny": _format_units(units, credit_store.UNITS_PER_CNY),
    }


# --------------------------------------------------------------------------- #
# 新用户后端与赠送（三条建号路径共用）
# --------------------------------------------------------------------------- #
def new_user_backend_tx(cur):
    """事务内读 ``platform_settings.credit_new_user_backend``。

    缺键/非法值一律 ``legacy``（不放大新权益面）。
    """
    cur.execute(
        "SELECT value FROM platform_settings WHERE key=%s",
        (NEW_USER_BACKEND_KEY,))
    row = cur.fetchone()
    value = row["value"] if row is not None else None
    return value if value in NEW_USER_BACKENDS else "legacy"


def provision_signup_wallet_tx(cur, user_id, limit_nano_cny, *,
                               actor_user_id=None):
    """建号路径同事务赠送（backend=credits 时）→ 账户 dict 或 None。

    - ``legacy``：no-op，返回 None（不建 billing_accounts 行）；
    - ``credits``：调用 :func:`credit_store.provision_credit_account_tx`
      （``grant_units=limit_nano_cny``、``source_key='signup:<user_id>'``、
      ``kind='promotional'``、reason ``signup grant``）。额度为 0 时只开
      credits 账户、不发零额批次（原语已如此）。
    - 幂等：同一 source_key 重放返回原批次，不重复发放。
    """
    user_id = _require_text(user_id, "user_id")
    limit = _require_nano(limit_nano_cny, "limit_nano_cny")
    if new_user_backend_tx(cur) != "credits":
        return None
    return credit_store.provision_credit_account_tx(
        cur, user_id, grant_units=limit,
        source_key=SIGNUP_SOURCE_KEY_PREFIX + user_id,
        reason=SIGNUP_REASON, kind="promotional",
        actor_user_id=actor_user_id)


# --------------------------------------------------------------------------- #
# plan：只读盘点
# --------------------------------------------------------------------------- #
def _open_legacy_holds_by_user_tx(cur):
    """每个 user 的 open legacy hold 数（按 subject 统计，含 account_id 空）。"""
    cur.execute(
        "SELECT subject_id, COUNT(*)::bigint AS n FROM billing_holds "
        "WHERE subject_type='user' AND status='open' "
        "AND billing_backend='legacy' GROUP BY subject_id")
    return {row["subject_id"]: int(row["n"]) for row in cur.fetchall()}


def _allowances_by_user_tx(cur):
    cur.execute(
        "SELECT allowance_id, subject_id, limit_nano_cny, spent_nano_cny, "
        "reserved_nano_cny, version, source, cutover_at "
        "FROM ai_spend_total_allowances WHERE subject_type='user'")
    return {row["subject_id"]: dict(row) for row in cur.fetchall()}


def _accounts_by_user_tx(cur):
    cur.execute(
        "SELECT account_id, user_id, billing_backend "
        "FROM billing_accounts ORDER BY user_id")
    return {row["user_id"]: dict(row) for row in cur.fetchall()}


def _classify(account, allowance, open_holds):
    """单用户 plan 状态（spec §10 的五种词表）。"""
    if account is not None \
            and account["billing_backend"] == credit_store.BACKEND_CREDITS:
        return "already_credits"
    if allowance is None:
        return "skip_no_allowance"
    if int(allowance["reserved_nano_cny"]) != 0:
        return "skip_reserved"
    if open_holds:
        return "skip_open_holds"
    return "ready"


def plan_rows(cur):
    """只读盘点每个 role=user 用户（含禁用）→ plan 行列表。

    每行：``user_id``、``limit_nano_cny``、``spent_nano_cny``、
    ``reserved_nano_cny``、``allowance_id``、``allowance_version``、
    ``open_legacy_holds``、``grant_units``（= max(0, limit − spent)，无
    总额度行时为 0）、``status``（:data:`PLAN_STATUSES`）。owner/demo 不在
    查询范围（role='user' 才是钱包主体）。
    """
    cur.execute(
        "SELECT user_id FROM users WHERE role='user' ORDER BY user_id")
    user_ids = [row["user_id"] for row in cur.fetchall()]
    allowances = _allowances_by_user_tx(cur)
    accounts = _accounts_by_user_tx(cur)
    open_holds = _open_legacy_holds_by_user_tx(cur)

    rows = []
    for user_id in user_ids:
        allowance = allowances.get(user_id)
        account = accounts.get(user_id)
        holds = open_holds.get(user_id, 0)
        if allowance is None:
            limit = spent = reserved = None
            allowance_id = None
            allowance_version = None
            grant_units = 0
        else:
            limit = int(allowance["limit_nano_cny"])
            spent = int(allowance["spent_nano_cny"])
            reserved = int(allowance["reserved_nano_cny"])
            allowance_id = allowance["allowance_id"]
            allowance_version = int(allowance["version"])
            grant_units = max(0, limit - spent)  # 1:1 at UNITS_PER_CNY
        rows.append({
            "user_id": user_id,
            "limit_nano_cny": limit,
            "spent_nano_cny": spent,
            "reserved_nano_cny": reserved,
            "allowance_id": allowance_id,
            "allowance_version": allowance_version,
            "open_legacy_holds": int(holds),
            "grant_units": grant_units,
            "status": _classify(account, allowance, holds),
        })
    return rows


def plan_hash(rows):
    """plan 行的 canonical SHA-256（按 user_id 排序，对七元组哈希）。"""
    ordered = sorted(rows, key=lambda row: row["user_id"])
    payload = [
        [
            row["user_id"],
            row["limit_nano_cny"],
            row["spent_nano_cny"],
            row["reserved_nano_cny"],
            row["open_legacy_holds"],
            row["grant_units"],
            row["status"],
        ]
        for row in ordered
    ]
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _plan_payload(rows):
    counts = {status: 0 for status in PLAN_STATUSES}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    ready_grant = sum(row["grant_units"] for row in rows
                      if row["status"] == "ready")
    return {
        "rows": rows,
        "counts": counts,
        "grant_units": int(ready_grant),
        "grant_credits": _format_units(
            ready_grant, credit_store.UNITS_PER_CREDIT),
        "grant_cny": _format_units(
            ready_grant, credit_store.UNITS_PER_CNY),
        "plan_hash": plan_hash(rows),
    }


def _plan_tx(cur):
    return _plan_payload(plan_rows(cur))


def plan():
    """只读 plan（REPEATABLE READ 单快照）→ rows/计数/合计/hash。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                return _plan_tx(cur)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# apply：维护闸 + 会话锁 + hash 校验 + 逐用户独立事务
# --------------------------------------------------------------------------- #
def _maintenance_enabled_tx(cur):
    """``ai_dispatch_maintenance`` 精确为 true 才放行（缺行/非法=false）。"""
    cur.execute(
        "SELECT value FROM platform_settings WHERE key=%s",
        (settings_store.AI_DISPATCH_MAINTENANCE_KEY,))
    row = cur.fetchone()
    return row is not None and row["value"] is True


def _acquire_session_lock(conn):
    """取会话级用户开通锁（与建号/兑换的 xact 锁同键互斥）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)",
                    (spend_store.USER_PROVISIONING_ADVISORY_LOCK_KEY,))
    conn.commit()


def _release_session_lock(conn):
    """释放会话级锁（异常不掩盖主流程；连接关闭自动兜底释放）。"""
    try:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)",
                        (spend_store.USER_PROVISIONING_ADVISORY_LOCK_KEY,))
        conn.commit()
    except Exception:
        pass


def _legacy_ledger_totals_tx(cur, account):
    """账户当前 legacy 流水合计/条数（无账户 → 0/0）。"""
    if account is None:
        return 0, 0
    cur.execute(
        "SELECT COALESCE(SUM(amount_nano_cny), 0)::bigint AS total, "
        "       COUNT(*)::bigint AS n "
        "  FROM billing_ledger_entries "
        " WHERE account_id=%s AND ledger_scope='legacy'",
        (account["account_id"],))
    row = cur.fetchone()
    return int(row["total"]), int(row["n"])


def _apply_one(conn, row, expected_hash, actor_user_id):
    """单个 ready 用户的独立事务；返回逐用户结果 dict（异常已由调用方兜底）。"""
    user_id = row["user_id"]
    grant = int(row["grant_units"])

    def run(cur):
        cur.execute(
            "SELECT allowance_id, limit_nano_cny, spent_nano_cny, "
            "       reserved_nano_cny, version "
            "  FROM ai_spend_total_allowances "
            " WHERE subject_id=%s FOR UPDATE", (user_id,))
        allowance = cur.fetchone()
        if allowance is None:
            return {"result": "changed_since_plan", "reason": "allowance_gone"}
        if int(allowance["reserved_nano_cny"]) != 0:
            return {"result": "changed_since_plan", "reason": "reserved"}
        open_holds = _open_legacy_holds_by_user_tx(cur).get(user_id, 0)
        if open_holds:
            return {"result": "changed_since_plan", "reason": "open_holds"}
        current_grant = max(0, int(allowance["limit_nano_cny"])
                            - int(allowance["spent_nano_cny"]))
        if current_grant != grant:
            return {"result": "changed_since_plan", "reason": "grant_changed"}

        account = credit_store.get_account_by_user_tx(cur, user_id)
        if account is not None \
                and account["billing_backend"] == credit_store.BACKEND_CREDITS:
            return {"result": "already_credits",
                    "account_id": account["account_id"]}

        legacy_sum, legacy_count = _legacy_ledger_totals_tx(cur, account)
        migration = {
            "legacy_grant_units": grant,
            "limit_nano_cny": int(allowance["limit_nano_cny"]),
            "spent_nano_cny": int(allowance["spent_nano_cny"]),
            "reserved_nano_cny": int(allowance["reserved_nano_cny"]),
            "allowance_id": allowance["allowance_id"],
            "allowance_version": int(allowance["version"]),
            "legacy_ledger_sum_nano": legacy_sum,
            "legacy_ledger_count": legacy_count,
            "plan_hash": expected_hash,
            "source_version": MIGRATION_SOURCE_VERSION,
            "migrated_by": actor_user_id,
        }
        account = credit_store.provision_credit_account_tx(
            cur, user_id, grant_units=grant,
            source_key=LEGACY_SOURCE_KEY_TEMPLATE % user_id,
            reason=MIGRATION_REASON, kind="legacy_grant",
            actor_user_id=actor_user_id, migration=migration)
        share_store_pg.record_audit_tx(
            cur, MIGRATION_AUDIT_ACTION,
            actor_user_id=actor_user_id, actor_role="owner",
            target_type="billing_account", target_id=account["account_id"],
            detail={
                "user_id": user_id,
                "grant_units": grant,
                "plan_hash": expected_hash,
            })
        return {
            "result": "migrated",
            "account_id": account["account_id"],
            "grant_units": grant,
            "legacy_ledger_sum_nano": legacy_sum,
            "legacy_ledger_count": legacy_count,
        }

    with pg_store.transaction(conn) as c:
        with c.cursor() as cur:
            outcome = run(cur)
    return outcome


def _aborted_report(aborted, *, expected_hash, plan_payload=None):
    summary = {
        "users": len((plan_payload or {}).get("rows") or []),
        "migrated": 0,
        "already_credits": 0,
        "skipped": 0,
        "changed_since_plan": 0,
        "errors": 0,
        "grant_units": 0,
        "grant_credits": "0",
        "grant_cny": "0",
    }
    return {
        "ok": False,
        "aborted": aborted,
        "expected_hash": expected_hash,
        "plan_hash": (plan_payload or {}).get("plan_hash"),
        "counts": (plan_payload or {}).get("counts"),
        "results": [],
        "summary": summary,
    }


def apply(expected_hash, *, actor_user_id):
    """按已确认 hash 执行迁移（可重跑；失败用户不影响其他用户）。

    前置（任一不满足 → 不写任何行）：
      1. ``ai_dispatch_maintenance`` 精确为 true；
      2. 重算 plan 的 hash == ``expected_hash``。

    持会话级用户开通锁期间，对每个 ``ready`` 行开独立事务：锁总额度行 →
    重查 reserved/grant/open legacy holds（任一变化 → ``changed_since_plan``）
    → 记录 legacy 流水快照 → 开户/切换 + legacy_grant 批次 + 迁移快照 →
    audit。单用户异常只回滚该用户并记 ``error``。
    """
    expected_hash = _require_text(expected_hash, "expected_hash")
    actor_user_id = _require_text(actor_user_id, "actor_user_id")

    conn = _connect()
    try:
        _acquire_session_lock(conn)
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    cur.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    maintenance = _maintenance_enabled_tx(cur)
                    plan_payload = _plan_tx(cur)
            if not maintenance:
                return _aborted_report(
                    "maintenance_off", expected_hash=expected_hash,
                    plan_payload=plan_payload)
            if plan_payload["plan_hash"] != expected_hash:
                return _aborted_report(
                    "hash_mismatch", expected_hash=expected_hash,
                    plan_payload=plan_payload)

            results = []
            for row in plan_payload["rows"]:
                status = row["status"]
                if status == "already_credits":
                    results.append({
                        "user_id": row["user_id"],
                        "status": status,
                        "result": "already_credits",
                        "grant_units": row["grant_units"],
                    })
                    continue
                if status != "ready":
                    results.append({
                        "user_id": row["user_id"],
                        "status": status,
                        "result": "skipped",
                        "grant_units": row["grant_units"],
                    })
                    continue
                try:
                    outcome = _apply_one(
                        conn, row, expected_hash, actor_user_id)
                except Exception as exc:  # 单用户失败只回滚该用户
                    results.append({
                        "user_id": row["user_id"],
                        "status": status,
                        "result": "error",
                        "grant_units": row["grant_units"],
                        "error": str(getattr(exc, "code",
                                             type(exc).__name__)),
                    })
                    continue
                results.append({
                    "user_id": row["user_id"],
                    "status": status,
                    "grant_units": row["grant_units"],
                    **outcome,
                })

            summary = {
                "users": len(results),
                "migrated": sum(1 for r in results
                                if r["result"] == "migrated"),
                "already_credits": sum(1 for r in results
                                       if r["result"] == "already_credits"),
                "skipped": sum(1 for r in results
                               if r["result"] == "skipped"),
                "changed_since_plan": sum(
                    1 for r in results
                    if r["result"] == "changed_since_plan"),
                "errors": sum(1 for r in results
                              if r["result"] == "error"),
            }
            migrated_grant = sum(r["grant_units"] for r in results
                                 if r["result"] == "migrated")
            summary.update(_units_payload(migrated_grant))
            return {
                "ok": summary["errors"] == 0
                      and summary["changed_since_plan"] == 0,
                "aborted": None,
                "expected_hash": expected_hash,
                "plan_hash": plan_payload["plan_hash"],
                "counts": plan_payload["counts"],
                "results": results,
                "summary": summary,
            }
        finally:
            _release_session_lock(conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# verify：迁移后只读复核
# --------------------------------------------------------------------------- #
def _issue(kind, *, user_id, account_id, ref=None, expected=None,
           actual=None):
    return {
        "kind": kind,
        "user_id": user_id,
        "account_id": account_id,
        "ref": ref,
        "expected": expected,
        "actual": actual,
    }


def _verify_account_tx(cur, account_row, issues, stats):
    user_id = account_row["user_id"]
    account_id = account_row["account_id"]
    migrated_at = account_row["credit_migrated_at"]
    snapshot = account_row["credit_migration"] or {}
    if not isinstance(snapshot, dict):
        issues.append(_issue(
            "migration_snapshot_invalid", user_id=user_id,
            account_id=account_id, expected="dict",
            actual=type(snapshot).__name__))
        snapshot = {}
    if account_row["billing_backend"] != credit_store.BACKEND_CREDITS:
        issues.append(_issue(
            "migration_account_not_credits", user_id=user_id,
            account_id=account_id, expected=credit_store.BACKEND_CREDITS,
            actual=account_row["billing_backend"]))

    expected_units = snapshot.get("legacy_grant_units")
    cur.execute(
        "SELECT lot_id, source_key, issued_units FROM credit_lots "
        "WHERE account_id=%s AND kind='legacy_grant' ORDER BY lot_id",
        (account_id,))
    lots = [dict(row) for row in cur.fetchall()]
    issued = sum(int(lot["issued_units"]) for lot in lots)
    if not _is_int(expected_units) or int(expected_units) != issued:
        issues.append(_issue(
            "migration_legacy_grant_units", user_id=user_id,
            account_id=account_id, ref="legacy_grant",
            expected=expected_units, actual=issued))
    expected_key = LEGACY_SOURCE_KEY_TEMPLATE % user_id
    for lot in lots:
        if lot["source_key"] != expected_key:
            issues.append(_issue(
                "migration_lot_source_key", user_id=user_id,
                account_id=account_id, ref=lot["lot_id"],
                expected=expected_key, actual=lot["source_key"]))

    expected_sum = snapshot.get("legacy_ledger_sum_nano")
    expected_count = snapshot.get("legacy_ledger_count")
    if not _is_int(expected_sum) or not _is_int(expected_count):
        issues.append(_issue(
            "migration_snapshot_incomplete", user_id=user_id,
            account_id=account_id, ref="legacy_ledger",
            expected="legacy_ledger_sum_nano/legacy_ledger_count",
            actual=None))
    else:
        cur.execute(
            "SELECT COALESCE(SUM(amount_nano_cny), 0)::bigint AS total, "
            "       COUNT(*)::bigint AS n "
            "  FROM billing_ledger_entries "
            " WHERE account_id=%s AND ledger_scope='legacy' "
            "   AND created_at < %s",
            (account_id, migrated_at))
        row = cur.fetchone()
        actual_sum = int(row["total"])
        actual_count = int(row["n"])
        if actual_sum != int(expected_sum) or actual_count != int(expected_count):
            issues.append(_issue(
                "migration_legacy_ledger_snapshot", user_id=user_id,
                account_id=account_id, ref="legacy_ledger",
                expected={"sum": int(expected_sum),
                          "count": int(expected_count)},
                actual={"sum": actual_sum, "count": actual_count}))

    # 迁移后只允许迟到的 legacy usage_debit；其余 legacy kind 均为异常。
    cur.execute(
        "SELECT entry_id, kind, amount_nano_cny FROM billing_ledger_entries "
        "WHERE account_id=%s AND ledger_scope='legacy' AND created_at >= %s "
        "ORDER BY entry_id", (account_id, migrated_at))
    late = [dict(row) for row in cur.fetchall()]
    late_usage = 0
    for entry in late:
        if entry["kind"] == "usage_debit":
            late_usage += 1
            continue
        issues.append(_issue(
            "migration_late_legacy_non_usage_debit", user_id=user_id,
            account_id=account_id, ref=entry["entry_id"],
            expected="usage_debit", actual=entry["kind"]))
    stats["late_legacy_usage_debit"] += late_usage


def _reconcile_summary(report):
    """只保留 kind/计数与涉及账户（不含金额明细以外的敏感字段）。"""
    by_kind = {}
    accounts = set()
    for item in report.get("discrepancies") or []:
        kind = item.get("kind")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if item.get("account_id"):
            accounts.add(item.get("account_id"))
    return {
        "ok": bool(report.get("ok")),
        "summary": report.get("summary") or {},
        "discrepancy_count": len(report.get("discrepancies") or []),
        "by_kind": by_kind,
        "account_ids": sorted(accounts),
    }


def verify():
    """复核所有已迁移账户 → ``{"ok", "issues", "summary", "reconcile"}``。

    检查：迁移快照的 ``legacy_grant_units`` = legacy_grant 批次发行合计、
    批次 source_key 为 ``legacy:<user_id>:v1``、迁移前 legacy 流水合计/条数
    与快照一致、迁移后 legacy 写入只允许迟到的 ``usage_debit``。另外附上
    :func:`credit_store.reconcile_report` 的差异摘要（kind/计数/账户）。
    ``ok`` 只由本函数的具体 issue 决定（迟到的 legacy usage_debit 是允许
    的迁移后事件，不计 issue）；``reconcile.ok`` 单独呈现。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cur.execute(
                    "SELECT account_id, user_id, billing_backend, "
                    "       credit_migrated_at, credit_migration "
                    "  FROM billing_accounts "
                    " WHERE credit_migrated_at IS NOT NULL "
                    " ORDER BY user_id")
                accounts = [dict(row) for row in cur.fetchall()]
                issues = []
                stats = {"late_legacy_usage_debit": 0}
                for account_row in accounts:
                    _verify_account_tx(cur, account_row, issues, stats)
    finally:
        conn.close()

    reconcile = credit_store.reconcile_report()
    reconcile_out = _reconcile_summary(reconcile)
    return {
        "ok": not issues,
        "issues": issues,
        "summary": {
            "migrated_accounts": len(accounts),
            "issue_count": len(issues),
            "late_legacy_usage_debit": stats["late_legacy_usage_debit"],
            "reconcile_ok": reconcile_out["ok"],
            "reconcile_discrepancy_count":
                reconcile_out["discrepancy_count"],
        },
        "reconcile": reconcile_out,
    }


# --------------------------------------------------------------------------- #
# owner 迁移：只迁 role=owner，发 100,000 Credits promotional，切 credits
# （docs/owner-parity-plan-20261106.md §A2）
# --------------------------------------------------------------------------- #
def _require_credits_amount(credits):
    if not _is_int(credits) or credits <= 0:
        raise CreditMigrationError("credits 需为正整数（Credits）")
    return int(credits)


def _owner_users_tx(cur, user_id=None):
    """选中的 owner 行（指定 user_id 时同时返回角色供状态判定）。"""
    if user_id:
        cur.execute(
            "SELECT user_id, role, disabled FROM users WHERE user_id=%s",
            (user_id,))
    else:
        cur.execute(
            "SELECT user_id, role, disabled FROM users WHERE role='owner' "
            "ORDER BY user_id")
    return [dict(row) for row in cur.fetchall()]


def _owner_grant_lot_tx(cur, account_id, user_id):
    """按 owner 来源键读赠送批次（不存在 → None）。"""
    if account_id is None:
        return None
    cur.execute(
        "SELECT lot_id, kind, issued_units, remaining_units "
        "  FROM credit_lots WHERE account_id=%s AND source_key=%s",
        (account_id, OWNER_SOURCE_KEY_TEMPLATE % user_id))
    row = cur.fetchone()
    return dict(row) if row is not None else None


def _owner_window_snapshot_tx(cur, user_id):
    """owner 旧月窗口快照（最新一行；只读，不建行；时间转 ISO 字符串）。"""
    cur.execute(
        "SELECT window_id, limit_nano_snapshot, spent_nano_cny, "
        "       reserved_nano_cny, window_start, window_end "
        "  FROM ai_spend_windows "
        " WHERE subject_type='owner' AND subject_id=%s "
        " ORDER BY window_start DESC LIMIT 1", (user_id,))
    row = cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    for key in ("limit_nano_snapshot", "spent_nano_cny", "reserved_nano_cny"):
        if out.get(key) is not None:
            out[key] = int(out[key])
    for key in ("window_start", "window_end"):
        value = out.get(key)
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
    return out


def _owner_usage_tx(cur, user_id, *, lock=False):
    """owner 名下有效切片的已用量汇总 → dict（迁移写 used_bytes 的唯一口径）。

    范围：``owner_user_id = <owner>`` 且 ``asset_state`` 不在
    :data:`OWNER_USAGE_EXCLUDED_ASSET_STATES`（排除已删除/删除中/暂存）。
    返回 ``used_bytes``（``accounted_bytes`` 合计，NULL 计 0）、
    ``accounted_slides``（有账切片数）、``unaccounted_slides``（无
    ``accounted_bytes`` 的历史切片数）与 ``total_slides``。

    ``lock=True`` 时先按 ``slide_id`` 顺序 ``FOR UPDATE`` 锁住这些切片行
    （与发布/删除结算同锁序），保证迁移写入 used_bytes 期间没有并发结算
    造成重复或漏计。
    """
    excluded = list(OWNER_USAGE_EXCLUDED_ASSET_STATES)
    if lock:
        cur.execute(
            "SELECT slide_id FROM slides "
            " WHERE owner_user_id=%s AND NOT (asset_state = ANY(%s)) "
            " ORDER BY slide_id FOR UPDATE", (user_id, excluded))
        cur.fetchall()
    cur.execute(
        "SELECT COALESCE(SUM(accounted_bytes), 0)::bigint AS used_bytes, "
        "       COUNT(*) FILTER (WHERE accounted_bytes IS NOT NULL)::bigint "
        "         AS accounted_slides, "
        "       COUNT(*) FILTER (WHERE accounted_bytes IS NULL)::bigint "
        "         AS unaccounted_slides, "
        "       COUNT(*)::bigint AS total_slides "
        "  FROM slides "
        " WHERE owner_user_id=%s AND NOT (asset_state = ANY(%s))",
        (user_id, excluded))
    row = cur.fetchone()
    return {
        "used_bytes": int(row["used_bytes"]),
        "accounted_slides": int(row["accounted_slides"]),
        "unaccounted_slides": int(row["unaccounted_slides"]),
        "total_slides": int(row["total_slides"]),
    }


def _owner_quota_tx(cur, user_id):
    """owner 存储配额行（不存在 → None；只读不建行）。"""
    cur.execute(
        "SELECT quota_bytes, extra_quota_bytes, used_bytes, reserved_bytes "
        "  FROM upload_user_quotas WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    if row is None:
        return None
    out = {key: int(value) for key, value in dict(row).items()}
    out["quota_bytes"] = out["quota_bytes"] + out["extra_quota_bytes"]
    return out


def _owner_plan_row_tx(cur, user, *, credits):
    """单个 owner 的 plan 行。"""
    user_id = user["user_id"]
    grant_units = credits * credit_store.UNITS_PER_CREDIT
    account = credit_store.get_account_by_user_tx(cur, user_id)
    backend = account["billing_backend"] if account is not None else None
    lot = _owner_grant_lot_tx(
        cur, account["account_id"] if account is not None else None,
        user_id)
    quota = _owner_quota_tx(cur, user_id)
    window = _owner_window_snapshot_tx(cur, user_id)
    usage = _owner_usage_tx(cur, user_id)
    if (user.get("role") or "") != "owner":
        status = "not_owner"
    elif backend == credit_store.BACKEND_CREDITS:
        if lot is not None and int(lot["issued_units"]) == grant_units:
            status = "already_migrated"
        else:
            status = "grant_mismatch"
    elif lot is not None:
        status = "grant_mismatch"
    else:
        status = "ready"
    return {
        "user_id": user_id,
        "role": user.get("role"),
        "disabled": bool(user.get("disabled")),
        "billing_backend": backend,
        "account_id": account["account_id"] if account is not None else None,
        "grant_units": grant_units,
        "grant_credits": credits,
        "existing_grant_units": (int(lot["issued_units"])
                                 if lot is not None else None),
        "legacy_window": window,
        "quota": quota,
        # 迁移将要写入的已用量（新行）或既有行已记录值（不覆盖）：口径见
        # _owner_usage_tx（排除 deleted/deleting/staging；NULL 无账不计字节）。
        "used_bytes": usage["used_bytes"],
        "accounted_slides": usage["accounted_slides"],
        "unaccounted_slides": usage["unaccounted_slides"],
        "total_slides": usage["total_slides"],
        # 验收断言：owner 已用量必须远低于 1 TiB 基础容量。无配额行时按
        # 即将写入的 used_bytes 判定；有行时 used+reserved 必须装得下。
        "quota_ok": (
            (usage["used_bytes"] <= OWNER_BASE_QUOTA_BYTES)
            if quota is None
            else quota["used_bytes"] + quota["reserved_bytes"]
            <= quota["quota_bytes"]),
        "status": status,
    }


def owner_plan_hash(rows, *, credits):
    """owner plan 的 canonical SHA-256（含 credits 参数与已用量口径）。

    ``used_bytes`` / ``accounted_slides`` / ``unaccounted_slides`` 入 hash：
    plan 之后若有切片发布/删除/结算，apply 会因 hash 不符中止（漂移可见）。
    """
    ordered = sorted(rows, key=lambda row: row["user_id"])
    payload = [
        [
            row["user_id"],
            row["role"],
            row["billing_backend"],
            row["existing_grant_units"],
            row["grant_units"],
            row["used_bytes"],
            row["accounted_slides"],
            row["unaccounted_slides"],
            row["status"],
            int(credits),
        ]
        for row in ordered
    ]
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _owner_plan_payload_tx(cur, *, user_id, credits):
    users = _owner_users_tx(cur, user_id)
    if user_id and not users:
        raise CreditMigrationError(
            "user_id 不存在：%s" % user_id)
    rows = [_owner_plan_row_tx(cur, user, credits=credits) for user in users]
    counts = {status: 0 for status in OWNER_PLAN_STATUSES}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    migrating = [row for row in rows
                 if row["status"] in ("ready", "already_migrated")]
    return {
        "rows": rows,
        "counts": counts,
        "credits": int(credits),
        "grant_units": sum(row["grant_units"] for row in rows
                           if row["status"] == "ready"),
        # 将要写入（或缺失行将补建）的已用量汇总（额度行口径见
        # _owner_usage_tx；只统计会被处理的 owner）。
        "usage": {
            "used_bytes": sum(row["used_bytes"] for row in migrating),
            "accounted_slides": sum(row["accounted_slides"]
                                    for row in migrating),
            "unaccounted_slides": sum(row["unaccounted_slides"]
                                      for row in migrating),
        },
        "quota_ok": all(row["quota_ok"] for row in rows),
        "plan_hash": owner_plan_hash(rows, credits=credits),
    }


def owner_plan(*, user_id=None, credits=OWNER_GRANT_CREDITS):
    """只读盘点 owner 迁移计划（dry-run 用；不写任何行）。

    每行带将要写入配额行的 ``used_bytes``（owner 名下
    ``asset_state NOT IN ('deleted','deleting','staging')`` 切片的
    ``accounted_bytes`` 合计）、``accounted_slides`` / ``unaccounted_slides``
    计数与 ``total_slides``；这三项已纳入 plan hash，plan 之后发生切片
    发布/删除会让 apply 以 hash 不符中止（漂移可见）。
    """
    credits = _require_credits_amount(credits)
    if user_id is not None:
        user_id = _require_text(user_id, "user_id")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                return _owner_plan_payload_tx(
                    cur, user_id=user_id, credits=credits)
    finally:
        conn.close()


def _ensure_owner_quota_row_tx(cur, user_id, *, used_bytes=0):
    """owner 基础容量行（1 TiB）：仅在不存在时创建 → ``"created"|"existing"``。

    新行同时写入 ``used_bytes``（owner 历史上配额豁免，名下切片从未记账；
    迁移按 ``slides.accounted_bytes`` 汇总补记，见 :func:`_owner_usage_tx`）。
    管理员/既有行一律不覆盖（幂等）；env 默认值保持 10 GiB 不变。
    """
    cur.execute(
        "INSERT INTO upload_user_quotas (user_id, quota_bytes, used_bytes) "
        "VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING "
        "RETURNING user_id",
        (user_id, OWNER_BASE_QUOTA_BYTES, int(used_bytes)))
    created = cur.fetchone() is not None
    return "created" if created else "existing"


def _owner_apply_one(conn, row, expected_hash, actor_user_id, credits):
    """单个 owner 的独立事务；返回结果 dict（异常由调用方兜底）。"""
    user_id = row["user_id"]
    grant_units = int(row["grant_units"])
    credits = int(credits)
    expected_used = int(row["used_bytes"])

    def run(cur):
        account = credit_store.get_account_by_user_tx(cur, user_id)
        if account is not None:
            account = credit_store.lock_account_tx(
                cur, account["account_id"])
        # 已用量必须在写配额行之前、同事务内重算：迁移后 used_bytes 就是
        # 存储条口径，不能停在 0。先按 slide_id 顺序锁切片行，避免与并发
        # 发布/删除结算重复或漏计；与 plan 值不一致 → changed_since_plan。
        usage = _owner_usage_tx(cur, user_id, lock=True)
        if usage["used_bytes"] != expected_used:
            return {"result": "changed_since_plan",
                    "reason": "usage_changed"}

        if account is not None \
                and account["billing_backend"] == credit_store.BACKEND_CREDITS:
            lot = _owner_grant_lot_tx(cur, account["account_id"], user_id)
            if lot is not None and int(lot["issued_units"]) == grant_units:
                quota_action = _ensure_owner_quota_row_tx(
                    cur, user_id, used_bytes=usage["used_bytes"])
                stored = _owner_quota_tx(cur, user_id) or {}
                return {"result": "already_migrated",
                        "account_id": account["account_id"],
                        "grant_units": grant_units,
                        "used_bytes": usage["used_bytes"],
                        "accounted_slides": usage["accounted_slides"],
                        "unaccounted_slides": usage["unaccounted_slides"],
                        "quota_action": quota_action,
                        "quota_used_bytes": stored.get("used_bytes"),
                        "quota_bytes": stored.get("quota_bytes")}
            return {"result": "changed_since_plan",
                    "reason": "credits_without_matching_grant"}

        # 迁移时刻 = 事务 now()（同事务内与 credit_migrated_at 完全一致，
        # verify 的 created_at < migrated_at 口径因此无边界抖动）。
        cur.execute("SELECT now() AS ts")
        migrated_at = cur.fetchone()["ts"]
        legacy_sum, legacy_count = 0, 0
        if account is not None:
            # 与 verify()/_verify_account_tx 完全同口径（created_at < 迁移时刻）
            cur.execute(
                "SELECT COALESCE(SUM(amount_nano_cny), 0)::bigint AS total, "
                "       COUNT(*)::bigint AS n "
                "  FROM billing_ledger_entries "
                " WHERE account_id=%s AND ledger_scope='legacy' "
                "   AND created_at < %s",
                (account["account_id"], migrated_at))
            legacy_row = cur.fetchone()
            legacy_sum = int(legacy_row["total"])
            legacy_count = int(legacy_row["n"])
        window = _owner_window_snapshot_tx(cur, user_id)
        migration = {
            # verify() 口径：owner 不转换旧窗口余额，legacy_grant 发行量为 0。
            "legacy_grant_units": 0,
            "legacy_ledger_sum_nano": legacy_sum,
            "legacy_ledger_count": legacy_count,
            "grant_credits": credits,
            "grant_units": grant_units,
            "promotional_units": grant_units,
            "source": OWNER_MIGRATION_SOURCE,
            "source_version": MIGRATION_SOURCE_VERSION,
            "legacy_window": window,
            "plan_hash": expected_hash,
            "migrated_by": actor_user_id,
            "migrated_at": migrated_at.isoformat(),
        }
        account = credit_store.provision_credit_account_tx(
            cur, user_id, grant_units=grant_units,
            source_key=OWNER_SOURCE_KEY_TEMPLATE % user_id,
            reason=OWNER_GRANT_REASON, kind="promotional",
            actor_user_id=actor_user_id, migration=migration)
        quota_action = _ensure_owner_quota_row_tx(
            cur, user_id, used_bytes=usage["used_bytes"])
        stored = _owner_quota_tx(cur, user_id) or {}
        share_store_pg.record_audit_tx(
            cur, OWNER_MIGRATION_AUDIT_ACTION,
            actor_user_id=actor_user_id, actor_role="owner",
            target_type="billing_account", target_id=account["account_id"],
            detail={
                "user_id": user_id,
                "grant_credits": credits,
                "grant_units": grant_units,
                "legacy_ledger_sum_nano": legacy_sum,
                "legacy_ledger_count": legacy_count,
                "quota_action": quota_action,
                "used_bytes": usage["used_bytes"],
                "accounted_slides": usage["accounted_slides"],
                "unaccounted_slides": usage["unaccounted_slides"],
                "plan_hash": expected_hash,
            })
        return {
            "result": "migrated",
            "account_id": account["account_id"],
            "grant_units": grant_units,
            "legacy_ledger_sum_nano": legacy_sum,
            "legacy_ledger_count": legacy_count,
            "quota_action": quota_action,
            "used_bytes": usage["used_bytes"],
            "accounted_slides": usage["accounted_slides"],
            "unaccounted_slides": usage["unaccounted_slides"],
            "quota_used_bytes": stored.get("used_bytes"),
            "quota_bytes": stored.get("quota_bytes"),
        }

    with pg_store.transaction(conn) as c:
        with c.cursor() as cur:
            outcome = run(cur)
    return outcome


def owner_apply(expected_hash, *, actor_user_id, user_id=None,
                credits=OWNER_GRANT_CREDITS, dry_run=False):
    """按已确认 hash 执行 owner 迁移（幂等；dry-run 只读）。

    前置（写路径；任一不满足 → 不写任何行）：
      1. ``ai_dispatch_maintenance`` 精确为 true（迁移期间无新调用）；
      2. 重算 owner plan 的 hash == ``expected_hash``。

    ``dry_run=True`` 时维护闸不参与判定（不写任何行），只返回 plan 预览。
    每个 owner 独立事务：锁账户 → **锁 owner 名下有效切片并重算
    used_bytes**（与 plan 值不符 → ``changed_since_plan``）→ 记录旧
    ledger/窗口快照 → 切 credits + 发行 100,000 Credits promotional 批次
    （source_key ``owner-promotional:<user_id>:v1``）→ 建 1 TiB 基础容量行
    并写入该 `used_bytes`（仅当缺失；既有行不覆盖）→ audit。finally 附
    ``credit_store.reconcile_report`` 摘要。

    回滚：0088 的 ``billing_accounts_backend_guard`` 明确禁止 credits →
    legacy（一次性迁移）；回滚按运维文档执行——停 AI 派发、确认没有 credits
    消费后恢复迁移前的数据库备份与旧镜像。见
    docs/credit-wallet-ops-runbook-20261027.md。
    """
    expected_hash = _require_text(expected_hash, "expected_hash")
    actor_user_id = _require_text(actor_user_id, "actor_user_id")
    credits = _require_credits_amount(credits)
    if user_id is not None:
        user_id = _require_text(user_id, "user_id")

    conn = _connect()
    try:
        if not dry_run:
            _acquire_session_lock(conn)
        try:
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    cur.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    maintenance = _maintenance_enabled_tx(cur)
                    plan_payload = _owner_plan_payload_tx(
                        cur, user_id=user_id, credits=credits)
            if dry_run:
                return {
                    "ok": True,
                    "dry_run": True,
                    "aborted": None,
                    "expected_hash": expected_hash,
                    "plan_hash": plan_payload["plan_hash"],
                    "hash_matches": (plan_payload["plan_hash"]
                                     == expected_hash),
                    "counts": plan_payload["counts"],
                    "rows": plan_payload["rows"],
                }
            if not maintenance:
                return _owner_aborted_report(
                    "maintenance_off", expected_hash=expected_hash,
                    plan_payload=plan_payload)
            if plan_payload["plan_hash"] != expected_hash:
                return _owner_aborted_report(
                    "hash_mismatch", expected_hash=expected_hash,
                    plan_payload=plan_payload)

            results = []
            for row in plan_payload["rows"]:
                status = row["status"]
                if status not in ("ready", "already_migrated"):
                    results.append({
                        "user_id": row["user_id"], "status": status,
                        "result": "skipped",
                        "grant_units": row["grant_units"],
                    })
                    continue
                try:
                    outcome = _owner_apply_one(
                        conn, row, expected_hash, actor_user_id, credits)
                except Exception as exc:  # 单 owner 失败只回滚该 owner
                    results.append({
                        "user_id": row["user_id"], "status": status,
                        "result": "error", "grant_units": row["grant_units"],
                        "error": str(getattr(exc, "code",
                                             type(exc).__name__)),
                    })
                    continue
                results.append({
                    "user_id": row["user_id"], "status": status,
                    "grant_units": row["grant_units"], **outcome,
                })
        finally:
            if not dry_run:
                _release_session_lock(conn)
    finally:
        conn.close()

    summary = {
        "owners": len(results),
        "migrated": sum(1 for r in results if r["result"] == "migrated"),
        "already_migrated": sum(
            1 for r in results if r["result"] == "already_migrated"),
        "skipped": sum(1 for r in results if r["result"] == "skipped"),
        "changed_since_plan": sum(
            1 for r in results if r["result"] == "changed_since_plan"),
        "errors": sum(1 for r in results if r["result"] == "error"),
    }
    migrated_grant = sum(r["grant_units"] for r in results
                         if r["result"] == "migrated")
    summary.update(_units_payload(migrated_grant))
    reconcile_out = None
    if summary["migrated"] or summary["already_migrated"]:
        reconcile_out = _reconcile_summary(credit_store.reconcile_report())
    return {
        "ok": summary["errors"] == 0 and summary["changed_since_plan"] == 0
              and (reconcile_out is None or reconcile_out["ok"]),
        "dry_run": False,
        "aborted": None,
        "expected_hash": expected_hash,
        "plan_hash": plan_payload["plan_hash"],
        "counts": plan_payload["counts"],
        "results": results,
        "summary": summary,
        "reconcile": reconcile_out,
    }


def _owner_aborted_report(aborted, *, expected_hash, plan_payload=None):
    return {
        "ok": False,
        "dry_run": False,
        "aborted": aborted,
        "expected_hash": expected_hash,
        "plan_hash": (plan_payload or {}).get("plan_hash"),
        "counts": (plan_payload or {}).get("counts"),
        "rows": (plan_payload or {}).get("rows") or [],
        "results": [],
        "summary": {
            "owners": len((plan_payload or {}).get("rows") or []),
            "migrated": 0, "already_migrated": 0, "skipped": 0,
            "changed_since_plan": 0, "errors": 0,
            "grant_units": 0, "grant_credits": "0", "grant_cny": "0",
        },
        "reconcile": None,
    }


# --------------------------------------------------------------------------- #
# 新用户后端切换
# --------------------------------------------------------------------------- #
def set_new_user_backend(value, *, actor_user_id):
    """写 ``platform_settings.credit_new_user_backend`` + 同事务 audit。"""
    if value not in NEW_USER_BACKENDS:
        raise CreditMigrationError(
            "value 需为 %s（收到 %r）" % (NEW_USER_BACKENDS, value))
    actor_user_id = _require_text(actor_user_id, "actor_user_id")
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT value FROM platform_settings WHERE key=%s",
                    (NEW_USER_BACKEND_KEY,))
                row = cur.fetchone()
                previous = row["value"] if row is not None else None
                cur.execute(
                    "INSERT INTO platform_settings "
                    "(key, value, updated_at, updated_by) "
                    "VALUES (%s, %s, now(), %s) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, "
                    "updated_at=now(), updated_by=EXCLUDED.updated_by",
                    (NEW_USER_BACKEND_KEY,
                     psycopg.types.json.Jsonb(value),
                     "credit-migration:%s" % actor_user_id))
                share_store_pg.record_audit_tx(
                    cur, NEW_USER_BACKEND_AUDIT_ACTION,
                    actor_user_id=actor_user_id, actor_role="owner",
                    target_type="platform_settings",
                    target_id=NEW_USER_BACKEND_KEY,
                    detail={
                        "from": previous if isinstance(previous, str) else None,
                        "to": value,
                    })
        return {"key": NEW_USER_BACKEND_KEY, "value": value,
                "previous": previous if isinstance(previous, str) else None}
    finally:
        conn.close()
