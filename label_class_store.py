# -*- coding: utf-8 -*-
"""标注类别仓储（P1-W1，PG 存储）。

模式对齐 llm_provider_store / settings_store：模块级函数 + 中文注释，
psycopg dict_row，事务走 pg_store.transaction，revision CAS（行锁内
expected_revision 比对，不符抛 :class:`RevisionConflictError`）。

语义（docs/annotation-classes-w1-decisions.md D1-D4）：
  - 全局类别（无 owner 隔离；owner 治理 CRUD，普通用户读启用类别——
    HTTP 层裁剪，本层不鉴权）；
  - 删除为软删除（``deleted=true``）：历史标注的 classification 快照
    原样保留，导出侧按「当前色 → 快照色 → 哈希色」降级；name 唯一由
    部分唯一索引 ``uq_label_classes_live_name``（WHERE NOT deleted）兜底，
    软删后允许重建同名；
  - color 权威恒取本表当前值；rois 只存 name 引用 + color 展示快照
    （写侧引用校验在本模块 :func:`resolve_class`，非外键）。

审计：owner CRUD 写 audit_events（action=label_class.*），detail 只含
class_id/name 等非敏感字段。
"""
from __future__ import annotations

import secrets
import time

import psycopg

import pg_store

#: JSONB color 的 JSON 形态（[[r,g,b]]；dict 行转 list 由 _row_to_dict 处理）
_MAX_NAME_LEN = 60

#: 类别查询结果进程内缓存（秒）——导出逐条 build_feature 查色，避免同批
#: N 标注 × N 次点查；TTL 内改色/新建对导出透明度足够（导出非实时口径，
#: D5：色取「导出时刻当前类别表」）。单 worker（gunicorn 预加载禁用语义
#: 同 _STATUS_CACHE 先例），跨进程偏差可接受。
_LOOKUP_TTL_SECONDS = 30.0

_lookup_cache = {"rows": None, "at": 0.0}


class StoreError(Exception):
    """基类（HTTP 层 400）。"""

class ValidationError(StoreError):
    """输入形状非法。"""

class DuplicateNameError(ValidationError):
    """类别名与存续类别重复（HTTP 400 duplicate_name）。"""

class NotFoundError(StoreError):
    """类别不存在（含已软删；HTTP 404）。"""

class RevisionConflictError(StoreError):
    """revision CAS 冲突（HTTP 409）。"""


def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _row_to_dict(row) -> dict:
    out = dict(row)
    out["color"] = _color_list(out.get("color"))
    out["created_at"] = (out["created_at"].timestamp()
                         if out.get("created_at") else None)
    out["updated_at"] = (out["updated_at"].timestamp()
                         if out.get("updated_at") else None)
    return out


def _color_list(value):
    """JSONB color → [r,g,b] 整型三元组；非法返回 [127,127,127] 灰兜底。"""
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            out = [int(v) for v in value]
        except (TypeError, ValueError):
            return [127, 127, 127]
        if all(0 <= v <= 255 for v in out):
            return out
    return [127, 127, 127]


def _audit(cur, action: str, detail: dict):
    cur.execute(
        "INSERT INTO audit_events (event_id, ts, actor_role, action, "
        "target_type, detail) VALUES (%s, now(), 'owner', %s, 'label_classes', %s) "
        "ON CONFLICT (event_id) DO NOTHING",
        ("aud_lc_" + secrets.token_hex(16), action,
         psycopg.types.json.Jsonb(detail)))
    _invalidate_lookup_cache()


def _invalidate_lookup_cache():
    _lookup_cache["rows"] = None
    _lookup_cache["at"] = 0.0


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

def _validate_name(name) -> str:
    if not isinstance(name, str):
        raise ValidationError("name 需为字符串")
    name = name.strip()
    if not (1 <= len(name) <= _MAX_NAME_LEN):
        raise ValidationError("name 长度需在 1-%d 之间" % _MAX_NAME_LEN)
    return name


def _validate_color(color) -> list:
    """[r,g,b] 整型三元组（JSON 数组或 JSONB dict 行解出后的 list）。"""
    if isinstance(color, dict):  # {"0":r,...}：psycopg 对 [r,g,b] 的容错形态
        try:
            color = [color[str(i)] for i in range(3)]
        except KeyError:
            raise ValidationError("color 需为 [r,g,b] 整型三元组") from None
    if not isinstance(color, (list, tuple)) or len(color) != 3:
        raise ValidationError("color 需为 [r,g,b] 整型三元组")
    out = []
    for v in color:
        if isinstance(v, bool) or not isinstance(v, (int, float)) \
                or int(v) != v:
            raise ValidationError("color 需为 [r,g,b] 整型三元组")
        out.append(int(v))
    if not all(0 <= v <= 255 for v in out):
        raise ValidationError("color 分量需在 0-255 之间")
    return out


def _validate_sort_order(sort_order) -> int:
    if isinstance(sort_order, bool) or not isinstance(sort_order, int):
        raise ValidationError("sort_order 需为整数")
    if not (0 <= sort_order <= 100000):
        raise ValidationError("sort_order 需在 0-100000 之间")
    return sort_order


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

def create_class(*, name, color, sort_order=0, enabled=True,
                 created_by="") -> dict:
    """新建类别；name 与存续类别重复 → ValidationError（duplicate 语义）。"""
    name = _validate_name(name)
    color = _validate_color(color)
    sort_order = _validate_sort_order(sort_order)
    cid = "lc_" + secrets.token_hex(12)
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO label_classes "
                    "(class_id, name, color, sort_order, enabled, created_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                    (cid, name, psycopg.types.json.Jsonb(color), sort_order,
                     bool(enabled), created_by))
                row = _row_to_dict(cur.fetchone())
                _audit(cur, "label_class.created",
                       {"class_id": cid, "name": name})
    except psycopg.errors.UniqueViolation as exc:
        raise DuplicateNameError("类别名称已存在") from exc
    finally:
        conn.close()
    return public_class(row)


def update_class(class_id: str, *, expected_revision=None, **fields) -> dict:
    """部分更新：name/color/sort_order/enabled 可更新；带 expected_revision
    时 CAS。软删行不可更新（404 语义，NotFoundError）。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT * FROM label_classes WHERE class_id=%s "
                            "FOR UPDATE", (class_id,))
                row = cur.fetchone()
                if row is None or row.get("deleted"):
                    raise NotFoundError("类别不存在")
                if expected_revision is not None \
                        and int(expected_revision) != int(row["revision"]):
                    raise RevisionConflictError("类别已被他人修改，请刷新后重试")

                sets, params = [], []
                if "name" in fields:
                    sets.append("name=%s")
                    params.append(_validate_name(fields["name"]))
                if "color" in fields:
                    sets.append("color=%s")
                    params.append(psycopg.types.json.Jsonb(
                        _validate_color(fields["color"])))
                if "sort_order" in fields:
                    sets.append("sort_order=%s")
                    params.append(_validate_sort_order(fields["sort_order"]))
                if "enabled" in fields:
                    sets.append("enabled=%s")
                    params.append(bool(fields["enabled"]))
                if not sets:
                    return public_class(_row_to_dict(row))
                sets.append("updated_at=now()")
                sets.append("revision=revision+1")
                params.append(class_id)
                cur.execute(
                    "UPDATE label_classes SET " + ", ".join(sets) +
                    " WHERE class_id=%s RETURNING *", tuple(params))
                out = _row_to_dict(cur.fetchone())
                _audit(cur, "label_class.updated",
                       {"class_id": class_id,
                        "fields": sorted(k for k in fields)})
    except psycopg.errors.UniqueViolation as exc:
        raise DuplicateNameError("类别名称已存在") from exc
    finally:
        conn.close()
    return public_class(out)


def delete_class(class_id: str, *, expected_revision=None) -> None:
    """软删除（D4）：置 deleted=true，行保留；历史标注快照不受影响。"""
    conn = _connect()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute("SELECT * FROM label_classes WHERE class_id=%s "
                            "FOR UPDATE", (class_id,))
                row = cur.fetchone()
                if row is None or row.get("deleted"):
                    raise NotFoundError("类别不存在")
                if expected_revision is not None \
                        and int(expected_revision) != int(row["revision"]):
                    raise RevisionConflictError("类别已被他人修改，请刷新后重试")
                cur.execute(
                    "UPDATE label_classes SET deleted=true, enabled=false, "
                    "updated_at=now(), revision=revision+1 WHERE class_id=%s",
                    (class_id,))
                _audit(cur, "label_class.deleted",
                       {"class_id": class_id, "name": row["name"]})
    finally:
        conn.close()


def list_classes(*, enabled_only=False) -> list:
    """存续类别（NOT deleted）；enabled_only 过滤启用；按 sort_order,name。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM label_classes WHERE NOT deleted %s "
                "ORDER BY sort_order, name"
                % ("AND enabled" if enabled_only else ""))
            rows = [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return [public_class(r) for r in rows]


def get_class(class_id: str):
    """公共视图单查（含已删为 None）；不存在返回 None。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM label_classes WHERE class_id=%s "
                        "AND NOT deleted", (class_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    return public_class(_row_to_dict(row)) if row else None


def public_class(row: dict) -> dict:
    """对外视图（列表/详情/下拉）：无内部字段。"""
    return {
        "class_id": row["class_id"],
        "name": row["name"],
        "color": list(row.get("color") or [127, 127, 127]),
        "sort_order": int(row.get("sort_order", 0)),
        "enabled": bool(row.get("enabled")),
        "revision": int(row.get("revision", 1)),
        "updated_at": row.get("updated_at"),
    }


# --------------------------------------------------------------------------- #
# 标注写侧引用校验 + 导出取色（D2/D5）
# --------------------------------------------------------------------------- #

def resolve_class(name):
    """标注写侧引用校验：name → 启用中的类别 {"name","color"}。

    命中返回 dict（rois 权威 dict 的 classification 值）；未命中返回 None
    （调用方决定「无类别」还是报 unknown_class）。查询走 TTL 缓存（标注
    保存/编辑频率低，30s 窗口内停用的类别仍可通过校验一次——与导出侧
    同一非实时口径，可接受）。
    """
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    for row in _lookup_rows():
        if row["name"] == name and row["enabled"]:
            return {"name": row["name"], "color": list(row["color"])}
    return None


def class_color_by_name(name):
    """导出取色（D5 第 1 优先级）：启用**或停用**的存续类别当前色。

    只排除软删行（软删 = 类别不存在，导出降级走标注自身快照）。
    未命中返回 None。
    """
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    for row in _lookup_rows():
        if row["name"] == name:
            return list(row["color"])
    return None


def _lookup_rows():
    """name/color 最小集缓存（resolve/class_color 共用）。"""
    now = time.monotonic()
    rows = _lookup_cache["rows"]
    if rows is None or now - _lookup_cache["at"] > _LOOKUP_TTL_SECONDS:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT name, color, enabled FROM label_classes "
                            "WHERE NOT deleted")
                rows = [
                    {"name": r["name"], "color": _color_list(r["color"]),
                     "enabled": bool(r["enabled"])}
                    for r in cur.fetchall()
                ]
        finally:
            conn.close()
        _lookup_cache["rows"] = rows
        _lookup_cache["at"] = now
    return rows


def reset_lookup_cache_for_tests():
    """测试隔离钩子：清空进程内取色缓存（跨用例防串扰）。"""
    _invalidate_lookup_cache()
