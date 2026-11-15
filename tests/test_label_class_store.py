# -*- coding: utf-8 -*-
"""P1-W1 label_class_store 测试（先写测试钉住合同，再实现）。

合同（docs/annotation-classes-w1-decisions.md D1/D4）：
  - CRUD：create 返回 lc_<24hex> 公共视图（无内部字段）；name 与**存续**
    类别重复 → DuplicateNameError（软删后可重建同名——部分唯一索引语义）；
  - revision CAS：expected_revision 不符 → RevisionConflictError（409 语义）；
  - 软删除：delete 后 get/list 不再返回；list_classes(enabled_only) 只启用；
  - resolve_class：启用类别命中 → {"name","color"}；停用/软删/未知 → None
    （标注写侧 unknown_class 判定依据）；非字符串/空串 → None；
  - class_color_by_name：停用存续类别仍给当前色（导出降级链第 1 优先级
    只排除软删）；软删/未知 → None；
  - 取色缓存：reset_lookup_cache_for_tests 清空后新建类别立即可见；
  - color 校验：非三元组/越界/浮点 → ValidationError；
  - owner CRUD 写 audit_events（action=label_class.*）。

默认内嵌 PostgreSQL（conftest 逐用例 TRUNCATE）。
运行：cd 项目根 && python3 -m pytest tests/test_label_class_store.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import pytest  # noqa: E402

import label_class_store as store  # noqa: E402
import pg_store  # noqa: E402
from _pt_helpers import isolate_app, pg_connection  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path)
    store.reset_lookup_cache_for_tests()
    yield
    store.reset_lookup_cache_for_tests()


def _create(**over):
    kw = dict(name="黏膜", color=[163, 42, 55], sort_order=10,
              created_by="u_owner")
    kw.update(over)
    return store.create_class(**kw)


# --------------------------------------------------------------------------- #
# CRUD 与唯一性
# --------------------------------------------------------------------------- #

def test_create_returns_public_view():
    row = _create()
    assert row["class_id"].startswith("lc_")
    assert row["name"] == "黏膜"
    assert row["color"] == [163, 42, 55]
    assert row["enabled"] is True
    assert isinstance(row["revision"], int) and row["revision"] >= 1
    # 公共视图无内部字段（created_by/deleted/created_at 不出）
    assert "created_by" not in row and "deleted" not in row


def test_duplicate_live_name_rejected():
    _create(name="平滑肌")
    with pytest.raises(store.DuplicateNameError):
        _create(name="平滑肌", color=[10, 10, 10])


def test_duplicate_name_allowed_after_soft_delete():
    row = _create(name="血管")
    store.delete_class(row["class_id"])
    reborn = _create(name="血管", color=[200, 30, 30])
    assert reborn["class_id"] != row["class_id"]
    assert reborn["color"] == [200, 30, 30]


def test_update_fields_and_revision_bump():
    row = _create()
    rev = row["revision"]
    out = store.update_class(row["class_id"], color=[0, 128, 0],
                             sort_order=5, enabled=False)
    assert out["color"] == [0, 128, 0]
    assert out["sort_order"] == 5
    assert out["enabled"] is False
    assert out["revision"] == rev + 1


def test_update_name_conflict_rejected():
    a = _create(name="神经")
    b = _create(name="坏死")
    with pytest.raises(store.DuplicateNameError):
        store.update_class(b["class_id"], name="神经")


# --------------------------------------------------------------------------- #
# CAS
# --------------------------------------------------------------------------- #

def test_cas_conflict():
    row = _create()
    with pytest.raises(store.RevisionConflictError):
        store.update_class(row["class_id"], expected_revision=99,
                           enabled=False)
    with pytest.raises(store.RevisionConflictError):
        store.delete_class(row["class_id"], expected_revision=99)
    # 正确 revision 通过
    out = store.update_class(row["class_id"], expected_revision=row["revision"],
                             enabled=False)
    assert out["enabled"] is False


# --------------------------------------------------------------------------- #
# 软删除与列表
# --------------------------------------------------------------------------- #

def test_soft_delete_hides_from_get_and_list():
    a = _create(name="黏膜")
    b = _create(name="平滑肌")
    store.delete_class(a["class_id"])
    assert store.get_class(a["class_id"]) is None
    names = [c["name"] for c in store.list_classes()]
    assert names == ["平滑肌"]
    # 行保留（软删）：库内仍可查（导出降级链按 name 不再命中）
    conn = pg_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT deleted FROM label_classes WHERE class_id=%s",
                        (a["class_id"],))
            assert cur.fetchone()["deleted"] is True
    finally:
        conn.close()
    assert store.get_class(b["class_id"])["name"] == "平滑肌"


def test_list_enabled_only():
    _create(name="黏膜")
    b = _create(name="平滑肌")
    store.update_class(b["class_id"], enabled=False)
    names = [c["name"] for c in store.list_classes(enabled_only=True)]
    assert names == ["黏膜"]
    # 全量（存续）含停用
    assert sorted(c["name"] for c in store.list_classes()) == \
        ["平滑肌", "黏膜"]


def test_delete_missing_raises_not_found():
    with pytest.raises(store.NotFoundError):
        store.delete_class("lc_" + "0" * 24)
    with pytest.raises(store.NotFoundError):
        store.update_class("lc_" + "0" * 24, enabled=False)


# --------------------------------------------------------------------------- #
# resolve_class / class_color_by_name（标注写侧 + 导出侧契约）
# --------------------------------------------------------------------------- #

def test_resolve_class_hit_disabled_and_soft_deleted():
    row = _create(name="神经", color=[90, 90, 200])
    hit = store.resolve_class("神经")
    assert hit == {"name": "神经", "color": [90, 90, 200]}
    # 停用 → 不再命中（标注写侧引用校验只认启用类别）
    store.update_class(row["class_id"], enabled=False)
    assert store.resolve_class("神经") is None
    # 软删 → 不命中
    store.update_class(row["class_id"], enabled=True)
    store.delete_class(row["class_id"])
    assert store.resolve_class("神经") is None


def test_resolve_class_invalid_inputs():
    assert store.resolve_class(None) is None
    assert store.resolve_class("") is None
    assert store.resolve_class("   ") is None
    assert store.resolve_class(123) is None
    assert store.resolve_class("不存在的类别") is None


def test_class_color_by_name_includes_disabled_excludes_deleted():
    row = _create(name="血管", color=[200, 30, 30])
    # 停用仍给色（导出恒用当前表色，D5）
    store.update_class(row["class_id"], enabled=False)
    assert store.class_color_by_name("血管") == [200, 30, 30]
    # 改色后给新色
    store.update_class(row["class_id"], enabled=True, color=[0, 255, 0])
    assert store.class_color_by_name("血管") == [0, 255, 0]
    # 软删 → None（降级到标注快照色）
    store.delete_class(row["class_id"])
    assert store.class_color_by_name("血管") is None


def test_lookup_cache_invalidated_on_create():
    # 缓存预热（空表）
    assert store.resolve_class("黏膜") is None
    _create(name="黏膜", color=[163, 42, 55])
    # create 后缓存失效 → 立即可见
    assert store.resolve_class("黏膜") == {"name": "黏膜",
                                           "color": [163, 42, 55]}


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

def test_color_validation():
    for bad in ([1, 2], [1, 2, 3, 4], "red", [1, 2, "x"],
                [-1, 0, 0], [0, 0, 256], [1.5, 2, 3], None):
        with pytest.raises(store.ValidationError):
            _create(name="坏色", color=bad)
    # 浮点整数值合法（1.0 == 1）
    row = _create(name="好色", color=[10.0, 20.0, 30.0])
    assert row["color"] == [10, 20, 30]


def test_name_validation():
    for bad in (None, 123, "", "   ", "x" * 61):
        with pytest.raises(store.ValidationError):
            _create(name=bad)
    with pytest.raises(store.ValidationError):
        _create(name="边界", sort_order=-1)
    with pytest.raises(store.ValidationError):
        _create(name="边界", sort_order=True)


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #

def test_audit_events_written():
    row = _create(name="黏膜")
    store.update_class(row["class_id"], enabled=False)
    store.delete_class(row["class_id"])
    conn = pg_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT action FROM audit_events "
                        "WHERE target_type='label_classes' ORDER BY ts")
            actions = [r["action"] for r in cur.fetchall()]
    finally:
        conn.close()
    assert actions == ["label_class.created", "label_class.updated",
                       "label_class.deleted"]
