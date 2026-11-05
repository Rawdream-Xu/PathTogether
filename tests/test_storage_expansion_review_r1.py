# -*- coding: utf-8 -*-
"""存储扩容复审 R1 复现用例（主代理审查，原样入仓作回归，不改断言）。

  1. 同一 Idempotency-Key 的两个并发购买：都应成功并指向同一份扩容
     （一个 duplicate=True），只扣一次费，不得抛出唯一约束异常；
  2. 账户被冻结（credit_frozen_reason）时到期续费：不扣款，进入宽限期；
  3. 对账 summary.storage_expansions 只统计扩容行，不把无扩容的配额行算进去。
"""
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_storage_expansion as base  # noqa: E402  (fixtures + helpers)
from test_storage_expansion import (  # noqa: E402,F401
    _isolate, _credits_user, _key, _purchase, _execute, _expansions,
    _entries, _quota_row, NOW0, UNIT_BYTES)

import credit_store  # noqa: E402
import storage_expansion as se  # noqa: E402
import upload_guard  # noqa: E402


def test_r1_same_key_concurrent_purchase_is_single_and_errorless():
    user, account = _credits_user(5000)
    key = _key()
    barrier = threading.Barrier(2)

    def buy():
        barrier.wait(timeout=10)
        return _purchase(user, 1, key=key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(buy), pool.submit(buy)]
        results = [f.result() for f in futures]  # 任一抛异常即失败
    ids = {r["expansion"]["expansion_id"] for r in results}
    assert len(ids) == 1
    assert sorted(bool(r["duplicate"]) for r in results) == [False, True]
    assert len(_expansions(user["user_id"])) == 1
    assert len(_entries(account["account_id"], "storage_debit")) == 1
    assert _quota_row(user["user_id"])["extra_quota_bytes"] == UNIT_BYTES


def test_r1_frozen_account_renewal_goes_to_grace_without_charge():
    user, account = _credits_user(5000)
    _purchase(user, 1, now=NOW0)
    _execute("UPDATE billing_accounts SET credit_frozen_reason='refund_dispute', "
             "credit_frozen_at=now() WHERE account_id=%s",
             (account["account_id"],))
    row = _expansions(user["user_id"])[0]
    stats = se.sweep(now=row["period_end"] + timedelta(seconds=1))
    assert stats["renewed"] == 0
    assert stats["entered_grace"] == 1
    after = _expansions(user["user_id"])[0]
    assert after["status"] == "grace"
    assert len(_entries(account["account_id"], "storage_debit")) == 1
    assert _quota_row(user["user_id"])["extra_quota_bytes"] == UNIT_BYTES


def test_r1_reconcile_summary_counts_only_expansion_rows():
    user, _account = _credits_user(2000)
    _purchase(user, 1)
    other, _other_account = _credits_user(10)
    upload_guard.get_quota_row(other["user_id"])  # 只建配额行，没有扩容
    report = credit_store.reconcile_report()
    assert report["ok"] is True, report["discrepancies"]
    assert report["summary"]["storage_expansions"] == 1
