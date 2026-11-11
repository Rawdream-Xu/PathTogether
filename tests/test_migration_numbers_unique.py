# -*- coding: utf-8 -*-
"""migrations/ 编号唯一性守卫（P0 收尾任务①，2026-11-12）。

背景：P0-W6 与 P1-W0 各自引入了一份 0094_* 迁移
（0094_ingestion_local_channel.sql / 0094_llm_providers.sql）造成重号。
ensure_schema 按 sorted(glob('*.sql')) 字典序应用（pg_store.py:142），且
schema_migrations 以文件名为主键——重号使账本无法表达唯一迁移序，违背
「一个编号一个迁移」惯例。部署库（本地 PG 5433）账本已同记两行，机制上
无害但违惯例，故将 0094_llm_providers.sql 重编为 0095_llm_providers.sql
（重放安全：该文件全幂等——CREATE TABLE IF NOT EXISTS / 约束按名判存 /
audit 固定 event_id ON CONFLICT DO NOTHING）。

守卫两条：
  1. migrations/*.sql 四位编号无重号（缺号允许——0085/0086 为历史缺号，
     缺号不破坏账本语义，重号才破坏）；
  2. llm_providers 迁移恰一份且为新编号 0095（钉住本次重编形态，防止
     回退 reintroduce 双 0094）。
"""
import re

import pg_store

_MIGRATIONS_DIR = pg_store.migrations_dir()
_NAME_RE = re.compile(r"^(\d{4})_[A-Za-z0-9_]+\.sql$")


def test_migration_numbers_unique():
    """migrations/ 下每个 .sql 的四位编号必须唯一（缺号允许）。"""
    numbers = []
    for p in sorted(_MIGRATIONS_DIR.iterdir()):
        m = _NAME_RE.match(p.name)
        assert m is not None, (
            "migrations/ 文件名不符合 NNNN_topic.sql 惯例: %s" % p.name)
        numbers.append(m.group(1))
    dupes = sorted({n for n in numbers if numbers.count(n) > 1})
    assert dupes == [], "migrations/ 迁移编号重号: %s" % dupes


def test_llm_providers_migration_single_renumbered():
    """llm_providers 迁移恰一份，且编号为重编后的 0095。"""
    hits = sorted(p.name for p in _MIGRATIONS_DIR.glob("*_llm_providers.sql"))
    assert hits == ["0095_llm_providers.sql"], (
        "llm_providers 迁移应为唯一一份 0095_llm_providers.sql，实际: %s" % hits)
