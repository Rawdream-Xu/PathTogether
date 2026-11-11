# -*- coding: utf-8 -*-
"""P1-W0a llm_provider_store 测试（先写测试钉住合同，再实现）。

合同（docs/llm-vision-w0-decisions.md D1/D2）：
  - api_key Fernet 加密落库（``enc:`` 前缀密文），库内**任何列**不含明文；
  - 公共视图（列表/详情）永不回显 api_key（含密文），只回显尾 4 位；
  - Fernet 不可用 → 新增/换 key 一律拒绝（fail-closed），保留旧 key 的更新不受影响；
  - base_url 校验：http/https、无 userinfo、拒绝 localhost/环回/私网/保留/
    链路本地/元数据字面量与 DNS 解析结果（SSRF 面，Mimosa 红线）；
    env LLM_VISION_ALLOW_PRIVATE_UPSTREAM=1 仅放开私网（环回永不放开）；
  - is_default 全局唯一（部分唯一索引兜底）；默认供应商禁用后解析返回 None
    （优雅降级，不自动改库）；
  - revision CAS：expected_revision 不匹配 → 409 语义异常；
  - owner CRUD 写 audit_events，detail 绝不含 api_key 材料；
  - provider_id 形如 lp_<24hex>；name 唯一。

全部用例不真实外呼：DNS 经 monkeypatch llm_vision._host_ips，无 HTTP。
测试用哑凭据每次运行随机生成（源码不落任何可用凭据字面量，Mimosa 红线）。
"""
import ipaddress
import json
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import psycopg  # noqa: E402
import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

import llm_provider_store as store  # noqa: E402
import llm_vision  # noqa: E402
import pg_store  # noqa: E402
from _pt_helpers import isolate_app, pg_connection  # noqa: E402


def _dummy_secret():
    """每次运行随机生成的哑凭据材料（长度>8，可测尾 4 位掩码）。"""
    return "test-" + secrets.token_hex(24)


PLAINTEXT = _dummy_secret()      # 主用例的哑 api_key
PLAINTEXT_2 = _dummy_secret()    # 换 key 用例的哑 api_key


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    isolate_app(monkeypatch, tmp_path)
    # 每用例注入独立 Fernet（真实加解密，密钥随机）；yield 出实例供
    # 「不可用→恢复」类用例回挂
    fer = Fernet(Fernet.generate_key())
    monkeypatch.setattr(store, "_fernet_factory", lambda: fer)
    # 默认拒绝私网；个别用例自行覆盖 env
    monkeypatch.delenv("LLM_VISION_ALLOW_PRIVATE_UPSTREAM", raising=False)
    yield fer


@pytest.fixture(autouse=True)
def _dns(monkeypatch):
    """测试 DNS：IP 字面量按字面；localhost/元数据 → 私网；其余给公网 IP。"""

    def fake_ips(hostname):
        h = (hostname or "").lower().rstrip(".")
        try:
            return [ipaddress.ip_address(h)]
        except ValueError:
            pass
        if h in ("localhost",) or h.endswith(".localhost"):
            return [ipaddress.ip_address("127.0.0.1")]
        if h in ("metadata.google.internal", "metadata.goog", "metadata"):
            return [ipaddress.ip_address("169.254.169.254")]
        if h == "internal.corp.example" or h.endswith(".internal"):
            return [ipaddress.ip_address("10.20.30.40")]
        return [ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr(llm_vision, "_host_ips", fake_ips)


def _create(**over):
    kw = dict(name="DeepSeek", base_url="https://api.deepseek.com/v1",
              api_key=PLAINTEXT, models=["deepseek-chat"],
              default_model="deepseek-chat", created_by="u_owner")
    kw.update(over)
    return store.create_provider(**kw)


# --------------------------------------------------------------------------- #
# 加密与回显红线
# --------------------------------------------------------------------------- #

def test_create_encrypts_api_key_no_plaintext_in_db():
    row = _create()
    assert row["provider_id"].startswith("lp_")
    conn = pg_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM llm_providers WHERE provider_id=%s",
                        (row["provider_id"],))
            raw = {k: str(v) for k, v in cur.fetchone().items()}
    finally:
        conn.close()
    assert raw["api_key_encrypted"].startswith("enc:")
    assert PLAINTEXT not in " ".join(raw.values()), "库内任何列不得出现明文 key"
    assert raw["api_key_tail4"] == PLAINTEXT[-4:]


def test_public_view_never_exposes_key():
    row = _create()
    pub = store.public_provider(row)
    assert "api_key" not in pub and "api_key_encrypted" not in pub
    assert pub["api_key_tail4"] == PLAINTEXT[-4:]
    for item in store.list_providers():
        assert "api_key" not in item and "api_key_encrypted" not in item


def test_provider_for_call_returns_plaintext_only_server_side():
    row = _create()
    full = store.get_provider_for_call(row["provider_id"])
    assert full["api_key"] == PLAINTEXT  # 仅服务端调用上游用


def test_fernet_unavailable_rejects_new_key_but_keeps_old(monkeypatch, _isolate):
    row = _create()
    monkeypatch.setattr(store, "_fernet_factory", lambda: None)
    with pytest.raises(store.SecretUnavailableError):
        _create(name="Another", base_url="https://api.openai.com/v1",
                api_key=PLAINTEXT_2, models=["gpt-4o"], default_model="gpt-4o")
    with pytest.raises(store.SecretUnavailableError):
        store.update_provider(row["provider_id"], api_key=PLAINTEXT_2)
    # 不动 key 的更新照常
    out = store.update_provider(row["provider_id"], enabled=False)
    assert out["enabled"] is False
    # 密钥恢复可用后旧密文照常解出（fail-closed 只拒绝新写入，不毁存量）
    monkeypatch.setattr(store, "_fernet_factory", lambda: _isolate)
    assert store.get_provider_for_call(row["provider_id"])["api_key"] == PLAINTEXT


# --------------------------------------------------------------------------- #
# base_url 校验（SSRF 红线）
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "ftp://api.deepseek.com/v1",
    "file:///etc/passwd",
    "not-a-url",
    "https://127.0.0.1/v1",
    "http://localhost/v1",
    "http://10.0.0.5/v1",
    "http://192.168.1.4/v1",
    "https://169.254.169.254/latest/meta-data",
    "https://[::1]/v1",
    "https://user:pw@api.deepseek.com/v1",
    "https://metadata.google.internal/v1",
    "https://internal.corp.example/v1",  # DNS → 私网（fake_ips）
    "",
])
def test_base_url_rejected(url):
    with pytest.raises(store.BaseUrlInvalidError):
        _create(name="Bad-%d" % (abs(hash(url)) % 100000), base_url=url)


@pytest.mark.parametrize("url", [
    "https://api.deepseek.com/v1",
    "http://api.deepseek.com/v1",
    "https://open.bigmodel.cn/api/paas/v4",
])
def test_base_url_accepted(url):
    row = _create(name="Ok-%d" % (abs(hash(url)) % 100000), base_url=url)
    assert row["base_url"] == url


def test_private_upstream_env_only_unlocks_private_not_loopback(monkeypatch):
    monkeypatch.setenv("LLM_VISION_ALLOW_PRIVATE_UPSTREAM", "1")
    # 私网字面量：env 放开（自建 vLLM 场景）
    assert store.validate_base_url("http://10.1.2.3:8000/v1") is not None
    # 环回/localhost/元数据：env 也不放开
    for bad in ("http://127.0.0.1:8000/v1", "http://localhost/v1",
                "https://169.254.169.254/v1"):
        with pytest.raises(store.BaseUrlInvalidError):
            store.validate_base_url(bad)


# --------------------------------------------------------------------------- #
# 默认供应商 / CAS / CRUD
# --------------------------------------------------------------------------- #

def test_set_default_exclusive_and_disabled_default_degrades():
    p1 = _create(name="P1")
    p2 = _create(name="P2")
    store.set_default(p2["provider_id"])
    got = store.get_default_provider()
    assert got["provider_id"] == p2["provider_id"]
    # 唯一性：库里只一行 is_default
    conn = pg_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM llm_providers WHERE is_default")
            assert cur.fetchone()["count"] == 1
    finally:
        conn.close()
    # 禁用默认 → 解析返回 None（优雅降级），is_default 标志保留在行上
    store.update_provider(p2["provider_id"], enabled=False)
    assert store.get_default_provider() is None
    store.update_provider(p2["provider_id"], enabled=True)
    assert store.get_default_provider()["provider_id"] == p2["provider_id"]


def test_no_default_returns_none():
    _create(name="Only")
    assert store.get_default_provider() is None


def test_update_revision_cas():
    row = _create(name="CAS")
    with pytest.raises(store.RevisionConflictError):
        store.update_provider(row["provider_id"], expected_revision=999,
                              name="CAS-9")
    # default_model 需与 models 同请求保持一致（换默认需同时扩 models）
    with pytest.raises(store.ValidationError):
        store.update_provider(row["provider_id"], default_model="m2")
    out = store.update_provider(row["provider_id"],
                                expected_revision=row["revision"],
                                models=["deepseek-chat", "m2"],
                                default_model="m2")
    assert out["revision"] == row["revision"] + 1
    assert out["default_model"] == "m2"


def test_update_blank_api_key_keeps_old():
    row = _create(name="KeepKey")
    out = store.update_provider(row["provider_id"], api_key="")
    assert out["api_key_tail4"] == PLAINTEXT[-4:]
    assert store.get_provider_for_call(row["provider_id"])["api_key"] == PLAINTEXT


def test_delete_provider_clears_default():
    p = _create(name="Del")
    store.set_default(p["provider_id"])
    store.delete_provider(p["provider_id"])
    assert store.get_default_provider() is None
    assert store.get_provider_for_call(p["provider_id"]) is None


def test_duplicate_name_rejected():
    _create(name="Dup")
    with pytest.raises(store.ValidationError):
        _create(name="Dup", base_url="https://api.openai.com/v1")


def test_models_must_be_list_of_strings():
    with pytest.raises(store.ValidationError):
        _create(name="BadModels", models="deepseek-chat")
    with pytest.raises(store.ValidationError):
        _create(name="BadModels2", models=["ok", 123])


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #

def test_audit_rows_written_without_key_material():
    row = _create(name="Audited")
    store.update_provider(row["provider_id"], models=["deepseek-chat", "m9"],
                          default_model="m9")
    store.set_default(row["provider_id"])
    store.delete_provider(row["provider_id"])
    conn = pg_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, detail FROM audit_events "
                "WHERE action LIKE 'llm_provider.%' ORDER BY ts")
            rows = cur.fetchall()
    finally:
        conn.close()
    actions = [r["action"] for r in rows]
    assert "llm_provider.created" in actions
    assert "llm_provider.updated" in actions
    assert "llm_provider.default_set" in actions
    assert "llm_provider.deleted" in actions
    blob = json.dumps([r["detail"] for r in rows], default=str)
    assert PLAINTEXT not in blob, "审计 detail 绝不含 api_key 材料"
    assert "enc:" not in blob
