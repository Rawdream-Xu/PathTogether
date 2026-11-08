# -*- coding: utf-8 -*-
"""Batch D2：站点匿名访问统计测试（site_stats_store + 0030，docs
review-2026-09-02-upload-user-limits-admin-ui-cleanup.md §3.4 / §4.4 /
§Batch D2 / §7.2 / §7.3 / §8.1-9）。

store 级（默认全跑；内嵌 PostgreSQL）：
  - allowlist：/、/demo、/register、/login 精确命中；/api/*、/static/*、
    /admin、/healthz、工作区/深路径（含分享 token、切片名、项目 ID）不落；
  - 口径门：非 2xx-3xx 不落；非 HTML 不落；query 含 token/资源 ID 键整条
    拒绝；utm_source 白名单外丢弃；其余 query 全丢弃；
  - referrer 只取 hostname；同站（统计白名单/PUBLIC_*/本机）归 direct；
    裸 IP 字面量 referrer（IPv4/IPv6/IPv4-mapped/带端口）同样归 direct
    （2026-10-15 口径修正：合法外部来源几乎不会是裸 IP），历史 IP 字面量行
    不改写但被 top_referrers 聚合排除、recent 读取口径映射 direct；
    自有/内部 referrer hostname（2026-10-18 口径修正：COS_FAST_RELAY_URL
    推导的中转域名 + SITE_STATS_INTERNAL_REFERRER_HOSTS 可选配置）与同站
    同口径归 direct，且与入口白名单语义分离（Host=中转域名仍被拒绝采集）、
    精确 hostname 匹配（根域/未列出子域不误伤）、历史存量行不改写仅在
    读取层排除/映射 direct；
  - 入口域名（review 2026-09-15）：SITE_STATS_ENTRY_HOSTS 独立白名单
    fail-closed——未配置/含非法项停止采集并告警；Host 缺失（None/空）或
    未命中（同机其它站点/裸 IP）静默拒绝；命中后 request_host 规范化
    （小写去端口尾点）落库；
  - bot：Googlebot/curl/headless → suspected_bot + bot_name；正常浏览器
    登录 → signed_in_human；匿名 → anonymous_human；bot 优先于登录态；
  - 去重与哈希：IPv4 同 /24 同哈希、跨 /24 不同；IPv6 /64 同理；
    IPv4-mapped IPv6 等价；跨日哈希改变（含 Asia/Shanghai 日界回归）；
    同 hash+page+10 分钟桶唯一约束生效、跨桶落新行（真实 PG）；
  - 落库形态：0030+0053 列固定 12 列（含 request_host）；行值无完整 IP/
    UA/query/token/资源 ID；
  - secret 缺失/权限错误：build_event/enqueue 降级不落事件且不抛
    （公开页面仍成功由 app.py 接线代理测；此处只证 store 不抛）；
    secret 绝不复用 session secret（SECRET_KEY 无回退）；
  - worker：未启动 enqueue False；队列满 False 不抛；DB 故障注入丢批不抛；
    start/stop 幂等；缺 STORAGE_BACKEND 时仍起 worker（Wave 3 后 PG 唯一）；
  - dashboard_stats：形状与钉死契约逐键一致；visits/unique_visitors/bots
    三分类互不混入（爬虫不进入匿名访客近似数）；只读——调用前后业务表与
    site 表行数不变、不触发清理；90 天窗口外事件不计入；聚合只计
    request_host 命中当前白名单的行（同机其它域名/历史 NULL 行隔离，
    legacy.d30_visits 单独计数）；top_referrers 固定排除疑似爬虫（R4
    2026-09-19 爬虫开关退役，top_referrers_with_bots 对照口径一并移除）；
    daily 每日趋势 = 近 7 天共 7 行、UTC+8 日界、缺日补 0、严格倒序
    （第 1 行 = 今天，review 2026-09-19 R4）；today/d7/d30 KPI 窗口不变
    （30 天 KPI 不被误切成 7 天）；白名单未配置时主口径恒空
    且 host_filter_configured=False；
  - purge_expired 只删 expires_at 到期行；
  - F4/R2-F5 每日保留任务接线（双跑纯单元，monkeypatch 注入）：R2-F5 拆分
    后 acquisition 段（_run_daily_retention_once）与 site stats 段
    （_run_site_stats_retention_once）为独立函数/线程/开关——各自独立
    try/except 互不拖垮；store 缺失（None）时 site stats 段静默跳过；
    ACQ 间隔=0（归因退役）不影响 site stats 段；

app.py 接线契约测试（``site_stats_app_wiring`` 标记，**默认启用、不 skip**；
app.py 接线由并行代理实施，接线落地前这些用例红属预期）：
  - GET /api/admin/v1/site-stats owner-only：匿名 401 / user 403 / owner 200
    且形状与钉死契约逐键一致；（PG）调用前后 site_visit_events 行数不变；
  - after_request 采集：公开 HTML GET / 落 home 事件；/healthz（JSON）不落。

运行：
  cd PathTogether && python3 -m pytest tests/test_site_stats.py -q
  python3 -m pytest tests/test_site_stats.py -q
"""
import json
import logging
import os
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import app as app_mod  # noqa: E402
import site_stats_store as sss  # noqa: E402
import user_store  # noqa: E402
from _pt_helpers import (
    isolate_app,
    pg_connection as _conn,
    make_client as _client,
)  # noqa: E402

# app.py 接线由并行代理实施；依赖它的用例打此标记但**默认启用**（不 skip），
# 接线未就绪时红属预期，不得删除。
site_stats_app_wiring = pytest.mark.site_stats_app_wiring

import psycopg  # noqa: E402

UTC = timezone.utc
BASE_TIME = datetime(2026, 9, 3, 2, 0, 0, tzinfo=UTC)
BASE_DAY = "2026-09-03"          # BASE_TIME 的 Asia/Shanghai 日期
_SECRET_BYTES = b"unit-test-secret-0123456789abcdef-site-stats"
_BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) Safari/605.1.15"

DASHBOARD_KEYS = {
    "generated_at", "today", "d7", "d30", "daily", "top_referrers",
    "top_pages", "top_countries", "recent",
    "visitor_kinds", "entry_hosts", "host_filter_configured", "legacy",
    "geo_configured", "geo_source",
}
WINDOW_KEYS = {"visits", "unique_visitors", "bots"}
DAILY_KEYS = {"date", "visits", "unique_visitors", "bots"}
RECENT_KEYS = {"occurred_at", "page_key", "request_host", "referrer_domain",
               "country_code", "visitor_kind", "bot_name"}
SITE_COLUMNS = [
    "event_id", "occurred_at", "dedup_bucket", "page_key",
    "referrer_domain", "utm_source", "country_code", "daily_visitor_hash",
    "visitor_kind", "bot_name", "expires_at", "request_host",
]
#: 采集启用前提（_make_secret 注入）：入口白名单默认两域名
ENTRY_HOSTS_DEFAULT = "histopilot.com,pt.solarise94.fun"


# --------------------------------------------------------------------------- #
# 公共基建
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例：存储隔离 + 采集 env 清理 + worker/告警节流状态复位。

    worker 跨用例必须停掉——上一个用例的 monkeypatch（如 _flush_batch 桩）
    在 teardown 已还原，残留线程会带着旧状态写库。
    """
    isolate_app(monkeypatch, tmp_path, clear_stores=True)
    for name in ("SITE_STATS_HMAC_SECRET_FILE", "PUBLIC_BASE_URL",
                 "PUBLIC_ORIGINS", "SERVER_NAME", "SITE_STATS_ENTRY_HOSTS",
                 "COS_FAST_RELAY_URL",
                 "SITE_STATS_INTERNAL_REFERRER_HOSTS",
                 "SITE_STATS_GEOIP_DB"):
        monkeypatch.delenv(name, raising=False)
    sss._reset_warn_state()
    sss._reset_geo_state()      # 离线国家库 reader 不跨用例泄漏（2026-11-08）
    sss.stop_worker()          # 先停 worker（app import 可能已自起；见 app.py）
    # conftest truncate 与 in-flight flush 之间可能落下前用例残余——
    # 本文件大量精确计数断言，显式清一次 site_visit_events 兜底
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM site_visit_events")
        conn.commit()
    finally:
        conn.close()
    # review R2-F2：app 接线用例会经 HTTP 建 owner/user（PG 上 role=user
    # 建号走「维护闸 + 开通锁」组合原语，闸 fail-closed）——conftest
    # TRUNCATE 清掉 0029 种子，每用例幂等重放（target=window + 闸=false）
    import _billing_helpers as bh
    bh.seed_spend_settings()
    yield
    sss.stop_worker()


@pytest.fixture
def secret(tmp_path, monkeypatch):
    """0600 secret 文件 + env 指向（采集启用的前提）。"""
    return _make_secret(tmp_path, monkeypatch)


def _make_secret(tmp_path, monkeypatch, mode=0o600,
                 content=_SECRET_BYTES, name="site_stats.secret"):
    p = tmp_path / name
    p.write_bytes(content)
    os.chmod(p, mode)
    monkeypatch.setenv("SITE_STATS_HMAC_SECRET_FILE", str(p))
    # 采集启用的另一前提：入口白名单（review 2026-09-15 fail-closed）
    monkeypatch.setenv("SITE_STATS_ENTRY_HOSTS", ENTRY_HOSTS_DEFAULT)
    return p


def _ev(**over):
    """build_event 固定入参（公开页成功 HTML GET、匿名、IPv4、入口域名
    histopilot.com——_make_secret 已注入白名单）。"""
    kwargs = dict(
        path="/", query_string="", referrer="", remote_addr="203.0.113.45",
        user_agent=_BROWSER_UA, status_code=200,
        content_type="text/html; charset=utf-8", signed_in=False,
        host="histopilot.com", now=BASE_TIME)
    kwargs.update(over)
    return sss.build_event(**kwargs)


def _fake_event(**over):
    """手工最小事件（绕开 secret，供队列/worker 语义测试）。"""
    ev = {
        "page_key": "home",
        "occurred_at": BASE_TIME.isoformat(),
        "dedup_bucket": int(BASE_TIME.timestamp() // sss.DEDUP_BUCKET_SECONDS),
        "request_host": "histopilot.com",
        "referrer_domain": "direct",
        "utm_source": None,
        "country_code": "unknown",
        "daily_visitor_hash": "a" * 64,
        "visitor_kind": "anonymous_human",
        "bot_name": None,
    }
    ev.update(over)
    return ev


def _sql_all(sql, params=()):
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _sql_one(sql, params=()):
    rows = _sql_all(sql, params)
    return rows[0] if rows else None


def _site_count():
    return _sql_one("SELECT count(*) AS n FROM site_visit_events")["n"]


def _business_counts():
    """业务表 + site 表行数（只读断言用；全部应为 0 或调用前后不变）。"""
    counts = {}
    for table in ("site_visit_events", "users", "ai_usage_events",
                  "billing_ledger_entries", "billing_holds",
                  "registration_invites", "platform_settings",
                  "ai_spend_windows"):
        counts[table] = _sql_one(
            "SELECT count(*) AS n FROM %s" % table)["n"]
    return counts


def _wait_until(pred, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())


def _flush(events):
    """直接驱动批量写（PG 测试不依赖 worker 线程时序）。"""
    sss._flush_batch(events)


# --------------------------------------------------------------------------- #
# 1. allowlist 与口径门（build_event 纯函数；双跑）
# --------------------------------------------------------------------------- #
def test_allowlist_pages_accepted_exact_match(secret):
    for path, page_key in sss.PAGE_ALLOWLIST.items():
        ev = _ev(path=path)
        assert ev is not None, path
        assert ev["page_key"] == page_key


def test_non_allowlist_paths_rejected(secret):
    # /api/*、静态资源、health、admin、登录后工作区、分享 token/切片名/
    # 项目 ID 深路径、尾斜杠与大小写变形——一律精确匹配失败
    for path in ("/api/demo/config", "/api/admin/v1/overview",
                 "/static/app.js", "/templates/base.html", "/healthz",
                 "/admin", "/admin/registration", "/workspace",
                 "/workspace/abc123", "/project/p-1", "/demo/slide-01",
                 "/share/tok123", "/r/campaign01", "/demo/", "/demo/x",
                 "//demo", "/DEMO", "/login/", "/register/step2", "",
                 "/?utm_source=x", None):
        assert _ev(path=path) is None, repr(path)


def test_status_and_content_type_gates(secret):
    for status in (100, 199, 404, 410, 500, 503, None, "200"):
        assert _ev(status_code=status) is None, status
    assert _ev(status_code=302) is not None          # 3xx 允许
    assert _ev(status_code=204, content_type=None) is None   # 非 HTML
    for ctype in ("application/json", "text/plain", "image/png",
                  "", None, "text/htmlscript"):
        assert _ev(content_type=ctype) is None, ctype
    assert _ev(content_type="Text/HTML; charset=utf-8") is not None


def test_sensitive_query_rejects_whole_event(secret):
    # query 含 token / 资源 ID / 凭证键 → 整条不落（最保守口径）
    for qs in ("?share_token=abc123", "?token=1", "?access_token=x",
               "?project_id=9", "?slide=slide-01", "?roi=42", "?id=7",
               "?api_key=SECRET", "?invite=cd9", "?session=sid",
               "?next_token=t", "?file=patient.tif"):
        assert _ev(query_string=qs) is None, qs


def test_query_otherwise_dropped_utm_whitelisted(secret):
    # 其余 query 全丢弃（事件仍落，但 utm 为空）；utm_source 仅白名单短标签
    ev = _ev(query_string="?foo=bar&utm_medium=cpc")
    assert ev is not None and ev["utm_source"] is None
    ev = _ev(query_string="?utm_source=newsletter")
    assert ev["utm_source"] == "newsletter"
    ev = _ev(query_string="?UTM_SOURCE=News")
    assert ev["utm_source"] == "News"
    # 超长 / 特殊字符 / 控制字符 / 空白 → 清洗为 None（事件保留）
    for bad in ("?utm_source=" + "x" * 40, "?utm_source=%3Cscript%3E",
                "?utm_source=a%00b", "?utm_source=a b"):
        ev = _ev(query_string=bad)
        assert ev is not None
        assert ev["utm_source"] is None, bad


def test_event_keys_are_exactly_contract_and_minimized(secret):
    ev = _ev(query_string="?utm_source=n", referrer="https://news.example.com")
    assert set(ev.keys()) == {
        "page_key", "occurred_at", "dedup_bucket", "request_host",
        "referrer_domain", "utm_source", "country_code", "daily_visitor_hash",
        "visitor_kind", "bot_name"}
    assert ev["request_host"] == "histopilot.com"
    blob = json.dumps(ev, ensure_ascii=False)
    # 原始 IP / UA / query / 资源标识一个都不能出现在事件里
    for raw in ("203.0.113.45", _BROWSER_UA, "utm_medium", "Safari/605",
                "share_token"):
        assert raw not in blob, raw


def test_referrer_hostname_only_and_same_site_direct(secret, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://histo.example.com")
    assert _ev(referrer="")["referrer_domain"] == "direct"
    assert _ev(referrer=None)["referrer_domain"] == "direct"
    assert _ev(referrer="garbage not a url")["referrer_domain"] == "direct"
    assert _ev(referrer="::::")["referrer_domain"] == "direct"
    ev = _ev(referrer="https://news.example.com/a/b?x=1#frag")
    assert ev["referrer_domain"] == "news.example.com"
    ev = _ev(referrer="HTTP://News.Example.COM:8443/x")
    assert ev["referrer_domain"] == "news.example.com"
    # 同站 hostname → direct（不当外部来源）
    assert _ev(referrer="https://histo.example.com/whatever")[
        "referrer_domain"] == "direct"
    assert _ev(referrer="https://histo.example.com")[
        "referrer_domain"] == "direct"
    monkeypatch.setenv(
        "PUBLIC_ORIGINS",
        "https://histopilot.com,https://histopilot.cn")
    assert _ev(referrer="https://histopilot.com/admin")[
        "referrer_domain"] == "direct"
    assert _ev(referrer="https://histopilot.cn/")[
        "referrer_domain"] == "direct"
    # 统计白名单域名也在"同站"集合里：两入口互跳不当外部来源
    assert _ev(referrer="https://pt.solarise94.fun/login", host="pt.solarise94.fun")[
        "referrer_domain"] == "direct"


def test_ip_literal_referrer_is_direct(secret):
    """裸 IP referrer 一律归 direct，不当外部来源（2026-10-15 口径修正）。

    理由（生产实例 2026-10-16）：合法外部来源几乎不会是裸 IP——referrer 为
    裸 IP 只可能来自互联网扫描器（直访边缘机裸 IP 后把该地址塞进 Referer，
    如 tencentfast 中转机 43.142.130.199 收到的 zgrab 扫描流量）或本服务
    自己的边缘/回源节点 IP，两者都不是「外部来源」，与同站跳转同口径归
    direct。覆盖 IPv4 / IPv6 / [::1] / IPv4-mapped / 带端口；正常域名
    referrer 不受影响（IPv6 不得被按冒号截断成 "2001" 之类的假来源）。"""
    cases = (
        "https://43.142.130.199/",            # 生产实际出现的扫描器来源（IPv4）
        "http://192.0.2.1:8443/a?b=c#f",      # IPv4 带端口
        "https://[2001:db8::1]/x",            # IPv6
        "https://[::1]:8443/",                # IPv6 [::1] 带端口
        "https://[::ffff:203.0.113.7]/",      # IPv4-mapped IPv6
        "https://[::FFFF:203.0.113.7]/",      # IPv4-mapped（大写十六进制）
    )
    for referrer in cases:
        ev = _ev(referrer=referrer)
        assert ev is not None, referrer
        assert ev["referrer_domain"] == "direct", referrer
    # 正常域名 referrer 不受影响；搜索来源照常保留（R5 口径不变）
    assert _ev(referrer="https://news.example.com/a?x=1")[
        "referrer_domain"] == "news.example.com"
    assert _ev(referrer="https://www.google.com/search?q=x")[
        "referrer_domain"] == "www.google.com"
    assert _ev(referrer="HTTP://News.Example.COM:8443/x")[
        "referrer_domain"] == "news.example.com"


def test_relay_referrer_is_direct_at_collection(secret, monkeypatch):
    """自有 COS fast 中转域名的 Referer 与同站跳转同口径归 direct（采集）。

    生产事实（review 2026-10-18）：COS_FAST_RELAY_URL=
    https://goapi.solarise94.fun，近 30 天 13 条 referrer_domain=
    goapi.solarise94.fun 事件（公开 HTML GET）把「外部来源 Top 10」榜污染。
    修复口径：中转域名是本服务自己的基础设施，不是外部来源——采集归
    direct；大小写/端口/尾点规范化后仍命中；真实外部来源（google/baidu）
    保留；solarise94.fun 根域与未列出子域不受影响（精确 hostname，不做
    后缀匹配）。"""
    monkeypatch.setenv("COS_FAST_RELAY_URL", "https://goapi.solarise94.fun")
    assert sss._internal_referrer_hosts() == {"goapi.solarise94.fun"}
    for referrer in ("https://goapi.solarise94.fun/",
                     "HTTPS://GoAPI.Solarise94.FUN/a?b=c#f",
                     "https://goapi.solarise94.fun.:8443/guide",
                     "http://GOAPI.solarise94.fun:8080/login"):
        ev = _ev(referrer=referrer)
        assert ev is not None, referrer
        assert ev["referrer_domain"] == "direct", referrer
    # 真实外部来源照常保留
    assert _ev(referrer="https://www.google.com/search?q=x")[
        "referrer_domain"] == "www.google.com"
    assert _ev(referrer="https://www.baidu.com/s?wd=x")[
        "referrer_domain"] == "www.baidu.com"
    # 精确 hostname：根域与未列出子域不是自有来源（不做后缀/父域匹配）
    assert _ev(referrer="https://solarise94.fun/")[
        "referrer_domain"] == "solarise94.fun"
    assert _ev(referrer="https://other.solarise94.fun/x")[
        "referrer_domain"] == "other.solarise94.fun"


def test_relay_host_still_rejected_as_request_host(secret, monkeypatch):
    """自有 referrer 集合绝不参与采集准入：Host=中转域名仍被入口白名单
    静默拒绝（review 2026-10-18 红线：目标域名决定「是不是本服务访问」，
    中转域名不是公开服务入口，绝不为此改 SITE_STATS_ENTRY_HOSTS /
    PUBLIC_ORIGINS）。"""
    monkeypatch.setenv("COS_FAST_RELAY_URL", "https://goapi.solarise94.fun")
    assert _ev(host="goapi.solarise94.fun") is None
    assert _ev(host="GOAPI.Solarise94.FUN:443") is None
    # 语义分离：入口白名单与公网入口集合都不含中转域名
    assert sss._stats_entry_hosts() == {"histopilot.com", "pt.solarise94.fun"}
    assert "goapi.solarise94.fun" not in sss._public_entry_hostnames()
    assert "goapi.solarise94.fun" not in sss._self_hostnames()


def test_internal_referrer_hosts_env_and_relay_derivation(
        secret, monkeypatch, caplog):
    """集合推导与可选专用配置 SITE_STATS_INTERNAL_REFERRER_HOSTS。

    - COS_FAST_RELAY_URL 未配置/畸形 → 空集贡献，不抛异常（采集/面板均
      不受影响，也没有任何来源被误排除）；
    - 专用配置每项容忍裸 hostname / 完整 origin / 带端口 / 尾点 / 大小写，
      统一规范化；与中转域名推导取并集；
    - 含非法项 → 整份作废（fail-closed，不"跳过坏项继续用"）+ 节流告警，
      中转域名推导不受影响，作废项也不再参与归类。"""
    # 未配置：空集，referrer 不受影响
    monkeypatch.delenv("COS_FAST_RELAY_URL", raising=False)
    monkeypatch.delenv("SITE_STATS_INTERNAL_REFERRER_HOSTS", raising=False)
    assert sss._internal_referrer_hosts() == set()
    assert sss._referrer_domain(
        "https://goapi.solarise94.fun/") == "goapi.solarise94.fun"
    # 畸形 URL：不抛异常，不贡献 hostname
    for bad in ("", "::::", "https://", "https://:8443"):
        monkeypatch.setenv("COS_FAST_RELAY_URL", bad)
        assert sss._internal_referrer_hosts() == set(), repr(bad)
    monkeypatch.setenv("COS_FAST_RELAY_URL", "https://goapi.solarise94.fun")
    # 专用配置：origin/大小写/端口/尾点规范化；与中转域名推导取并集；
    # 空项忽略
    monkeypatch.setenv("SITE_STATS_INTERNAL_REFERRER_HOSTS",
                       " HTTPS://Relay2.Example.COM:8443., "
                       "relay3.example.com ,,")
    assert sss._internal_referrer_hosts() == {
        "goapi.solarise94.fun", "relay2.example.com", "relay3.example.com"}
    assert sss._referrer_domain("https://relay2.example.com/x") == "direct"
    # 配置非法项：整份作废 + 节流告警；中转域名推导不受影响
    sss._reset_warn_state()
    monkeypatch.setenv("SITE_STATS_INTERNAL_REFERRER_HOSTS",
                       "relay2.example.com,bad host!!")
    with caplog.at_level(logging.WARNING, logger="svs.site_stats"):
        assert sss._internal_referrer_hosts() == {"goapi.solarise94.fun"}
    assert "SITE_STATS_INTERNAL_REFERRER_HOSTS" in caplog.text
    # 作废的配置项不再参与归类（未配置的 relay3 同样不受影响）
    assert sss._referrer_domain(
        "https://relay2.example.com/") == "relay2.example.com"
    assert sss._referrer_domain(
        "https://relay3.example.com/") == "relay3.example.com"
    # 采集准入与专用配置无关（语义分离红线）：relay2 作废后也不再被归类，
    # 但作为 Host 仍不在入口白名单、拒绝采集；正常入口 + relay referrer
    # 照常落事件（正对照，证明 None 来自准入而非 secret 缺失）
    assert _ev(host="relay2.example.com",
               referrer="https://relay2.example.com/") is None
    ev = _ev(referrer="https://relay3.example.com/")
    assert ev is not None
    assert ev["referrer_domain"] == "relay3.example.com"


def test_foreign_host_on_same_server_is_not_recorded(secret):
    """入口域名白名单（review 2026-09-15）：目标域名决定「是不是本服务访问」。

    同机其它站点（cpa.ni-biolab.com）/ 裸 IP / 伪造 Host 一律不落；
    本服务入口照常落库且 request_host 为规范化 hostname。"""
    assert _ev(host="cpa.ni-biolab.com") is None
    assert _ev(host="cpa.ni-biolab.com:443") is None
    assert _ev(host="") is None
    assert _ev(host="192.0.2.44") is None               # 裸 IP 访问
    assert _ev(host="evil.histopilot.com") is None      # 子域不在白名单
    assert _ev(host="histopilot.com.evil.example") is None
    # Host 未接线（None）也拒绝：app.py 恒传 request.host，缺失即异常路径
    assert _ev(host=None) is None
    # 本服务入口仍记；request_host 规范化（大写/端口/尾点 → 小写裸域名）
    ev = _ev(host="histopilot.com")
    assert ev is not None and ev["request_host"] == "histopilot.com"
    ev = _ev(host="HISTOPILOT.COM:443")
    assert ev is not None and ev["request_host"] == "histopilot.com"
    ev = _ev(host="PT.Solarise94.FUN.")
    assert ev is not None and ev["request_host"] == "pt.solarise94.fun"


def test_entry_host_allowlist_fail_closed(secret, monkeypatch, caplog):
    """SITE_STATS_ENTRY_HOSTS 未配置 / 含非法项 → 停止采集并节流告警；
    白名单外的 Host 拒绝**不**产生日志（外部扫描是常态，不刷屏）。"""
    # 未配置 → fail-closed + 告警
    monkeypatch.delenv("SITE_STATS_ENTRY_HOSTS", raising=False)
    sss._reset_warn_state()
    with caplog.at_level(logging.WARNING, logger="svs.site_stats"):
        assert _ev() is None
    assert "SITE_STATS_ENTRY_HOSTS" in caplog.text
    # 含非法项 → 整个白名单作废（不"跳过坏项继续用"）
    caplog.clear()
    sss._reset_warn_state()
    monkeypatch.setenv("SITE_STATS_ENTRY_HOSTS", "histopilot.com,bad host!!")
    with caplog.at_level(logging.WARNING, logger="svs.site_stats"):
        assert _ev() is None
        assert _ev(host="histopilot.com") is None
    assert "SITE_STATS_ENTRY_HOSTS" in caplog.text
    # 白名单生效但 Host 未命中 → 静默拒绝（无日志）
    caplog.clear()
    sss._reset_warn_state()
    monkeypatch.setenv("SITE_STATS_ENTRY_HOSTS", ENTRY_HOSTS_DEFAULT)
    with caplog.at_level(logging.WARNING, logger="svs.site_stats"):
        assert _ev(host="cpa.ni-biolab.com") is None
    assert caplog.text == ""


# --------------------------------------------------------------------------- #
# 2. 三分类与 bot 词表
# --------------------------------------------------------------------------- #
def test_ruleset_version_constant_shape():
    assert isinstance(sss.SITE_BOT_UA_RULESET_VERSION, str)
    assert sss.SITE_BOT_UA_RULESET_VERSION
    # R5（2026-09-19）：词表收窄（sogou/whatsapp）必须提版——版本历史与
    # 前后口径见 site_stats_store.SITE_BOT_UA_RULESET_VERSION 注释
    assert sss.SITE_BOT_UA_RULESET_VERSION == "2026-10-12.v4"


def test_bot_ua_classified_with_name(secret):
    cases = {
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)":
            "Googlebot",
        "Mozilla/5.0 (compatible; bingbot/2.0)": "Bingbot",
        "curl/8.4.0": "curl",
        "Wget/1.21": "Wget",
        "python-requests/2.31": "python-requests",
        "Mozilla/5.0 HeadlessChrome/120.0": "HeadlessBrowser",
        "Mozilla/5.0 (X11; Linux) AppleWebKit/537.36 PhantomJS/2.1":
            "PhantomJS",
        "Mozilla/5.0 (compatible; SomeCompanyCrawler/1.0)": "generic_bot",
        "facebookexternalhit/1.1": "FacebookExternalHit",
    }
    for ua, expected in cases.items():
        ev = _ev(user_agent=ua)
        assert ev is not None, ua
        assert ev["visitor_kind"] == "suspected_bot", ua
        assert ev["bot_name"] == expected, ua


def test_human_kinds_and_bot_priority(secret):
    ev = _ev(signed_in=False)
    assert ev["visitor_kind"] == "anonymous_human"
    assert ev["bot_name"] is None
    ev = _ev(signed_in=True)
    assert ev["visitor_kind"] == "signed_in_human"
    assert ev["bot_name"] is None
    # 带会话的爬虫仍归 suspected_bot（bot 判定优先）
    ev = _ev(user_agent="curl/8.4.0", signed_in=True)
    assert ev["visitor_kind"] == "suspected_bot"
    assert ev["bot_name"] == "curl"


# --------------------------------------------------------------------------- #
# 2b. R5（2026-09-19）：UA 样本表（脱敏完整 UA + 预期分类）
#
# 覆盖：搜狗浏览器 vs 搜狗爬虫、普通浏览器 vs Googlebot、链接预览抓取 vs
# 内置浏览器、自动化工具、空 UA。每组附证据说明（为何应这样分类）。
# 分类函数只依赖 UA 子串，store 级纯函数断言；不涉任何真实用户数据。
# --------------------------------------------------------------------------- #
#: (组名, 完整 UA, 预期 _classify_user_agent 结果, 证据)
UA_FIXTURES = (
    # --- 普通浏览器（必须保持 human，不因厂商词/来源误判）---
    ("chrome-desktop",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
     " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
     None, "最常见桌面 Chrome，无任何爬虫标识"),
    ("safari-macos",
     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
     " (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
     None, "桌面 Safari"),
    ("firefox-desktop",
     "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
     None, "桌面 Firefox"),
    ("sogou-mobile-browser",
     "Mozilla/5.0 (Linux; U; Android 13; zh-cn; M2012K11AC Build/TKQ1.220829."
     "002) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/108.0."
     "5359.82 Mobile Safari/537.36 SogouMobileBrowser/6.0",
     None, "v1 实测误判样本：搜狗手机浏览器 UA 带厂商词 sogou，"
           "v2 收窄后不再命中 SogouSpider（爬虫标识是 spider 而非 browser）"),
    ("sogou-desktop-browser",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
     " (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36 SE 2.X"
     " MetaSr 1.0",
     None, "搜狗高速浏览器（桌面）UA 带 SE 2.X MetaSr 1.0，无爬虫标识"),
    ("wechat-inapp",
     "Mozilla/5.0 (Linux; Android 13; PGT-AL10 Build/HUAWEIPGT-AL10;"
     " wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0"
     " Chrome/114.0.0.0 Mobile Safari/537.36 XWEB/85000917"
     " MicroMessenger/8.0.49",
     None, "微信内置浏览器：用户真实浏览，不是抓取"),
    ("whatsapp-inapp",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X)"
     " AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
     " WhatsApp/24.9.0.80",
     None, "WhatsApp 内置浏览器：UA 以 Mozilla/5.0 开头且带 WhatsApp 厂商"
           "词——v1 会误判为链接预览抓取，v2 用 mozilla/ 排除子串收窄"),
    # --- 已知爬虫 / 链接预览抓取（必须判为 suspected_bot + 具体名）---
    ("googlebot",
     "Mozilla/5.0 (compatible; Googlebot/2.1;"
     " +http://www.google.com/bot.html)",
     "Googlebot", "Google 官方搜索爬虫"),
    ("bingbot",
     "Mozilla/5.0 (compatible; bingbot/2.0;"
     " +http://www.bing.com/bingbot.htm)",
     "Bingbot", "Bing 官方爬虫"),
    ("sogou-web-spider",
     "Sogou web spider/4.0(+http://www.sogou.com/docs/help/webmasters.htm"
     "#07)",
     "SogouSpider", "搜狗官方网页爬虫（具体 spider 标识）"),
    ("sogou-spider-compat",
     "Mozilla/5.0 (compatible; SogouSpider/1.0;"
     " +https://www.sogou.com/docs/help/webmasters.html)",
     "SogouSpider", "搜狗爬虫的 Mozilla 包装形态"),
    ("sogou-inst-spider",
     "Sogou inst spider, contact spider@sogou.com",
     "SogouSpider", "搜狗收录爬虫变体"),
    ("whatsapp-link-preview",
     "WhatsApp/2.19.81 A",
     "WhatsApp", "WhatsApp 链接预览抓取：裸 UA（无 Mozilla/ 前缀），"
                 "与内置浏览器 UA 形态可区分"),
    ("facebook-link-preview",
     "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext"
     ".php)",
     "FacebookExternalHit", "Facebook 链接预览抓取（专用抓取 UA，非浏览器）"),
    ("twitterbot-preview",
     "Twitterbot/1.0",
     "Twitterbot", "Twitter/X 链接预览抓取"),
    # --- 自动化 / 采集工具 ---
    ("headless-chrome",
     "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like"
     " Gecko) HeadlessChrome/120.0.0.0 Safari/537.36",
     "HeadlessBrowser", "无头浏览器（自动化）"),
    ("curl", "curl/8.4.0", "curl", "命令行 HTTP 客户端"),
    ("python-requests", "python-requests/2.31.0", "python-requests",
     "Python HTTP 库"),
    ("generic-company-crawler",
     "Mozilla/5.0 (compatible; SomeCompanyCrawler/1.0)",
     "generic_bot", "未知公司爬虫：未命中具体词表，但命中泛化 crawl 标记"),
    # --- 空 UA（口径局限，不是已验证真人）---
    ("empty-ua", "", None, "空 UA：词表未命中 ≠ 已验证真人——下游归"
                           " anonymous_human 是记录在案的口径局限"
                           "（不新增 unknown 维度，见 _classify_user_agent "
                           "docstring）"),
)


@pytest.mark.parametrize("group,ua,expected,evidence", UA_FIXTURES,
                         ids=[f[0] for f in UA_FIXTURES])
def test_ua_fixture_table_classifications(group, ua, expected, evidence):
    """R5 样本表：每条脱敏完整 UA 的预期分类逐条钉死（id=组名）。"""
    assert sss._classify_user_agent(ua) == expected, group


def _ua(group):
    """按组名取样本表中的完整 UA（避免脆弱的位置索引）。"""
    for g, ua, _expected, _ev_reason in UA_FIXTURES:
        if g == group:
            return ua
    raise AssertionError("unknown UA fixture group: %s" % group)


def test_sogou_browser_human_and_sogou_spider_bot(secret):
    """R4/R5 核心回归：搜狗浏览器不再被误判为爬虫；搜狗爬虫仍被抓出。"""
    ev = _ev(user_agent=_ua("sogou-mobile-browser"))
    assert ev is not None
    assert ev["visitor_kind"] == "anonymous_human"
    assert ev["bot_name"] is None
    ev = _ev(user_agent=_ua("sogou-web-spider"))
    assert ev["visitor_kind"] == "suspected_bot"
    assert ev["bot_name"] == "SogouSpider"


def test_whatsapp_inapp_human_and_link_preview_bot(secret):
    """R5：WhatsApp 内置浏览器（Mozilla 形态）保持 human；裸 UA 链接预览
    抓取仍判 WhatsApp bot——不能把每次内置浏览器访问当抓取。"""
    ev = _ev(user_agent=_ua("whatsapp-inapp"))
    assert ev["visitor_kind"] == "anonymous_human"
    assert ev["bot_name"] is None
    ev = _ev(user_agent=_ua("whatsapp-link-preview"))
    assert ev["visitor_kind"] == "suspected_bot"
    assert ev["bot_name"] == "WhatsApp"


def test_empty_ua_limitation_documented(secret):
    """空 UA 现状：返回 None → 下游归 anonymous_human。这是口径局限
    （词表未命中 ≠ 已验证真人），按 review §7 保持现状并在代码注释/
    文档写明，不擅自当成确定爬虫，也不新增 unknown 维度。"""
    assert sss._classify_user_agent("") is None
    assert sss._classify_user_agent(None) is None
    assert sss._classify_user_agent("   ") is None
    ev = _ev(user_agent="")
    assert ev["visitor_kind"] == "anonymous_human"
    assert ev["bot_name"] is None


def test_search_referrer_from_human_browser_is_kept(secret):
    """R5 行为契约：正常浏览器 UA + google/baidu Referer 保留外部来源，
    访客为 human——不能把所有搜索来源当爬虫（来源=Referer、类别=UA，
    两个维度独立）。"""
    for referrer, domain in (("https://www.google.com/search?q=x", "www.google.com"),
                             ("https://www.baidu.com/s?wd=x", "www.baidu.com")):
        ev = _ev(referrer=referrer)
        assert ev["referrer_domain"] == domain, referrer
        assert ev["visitor_kind"] == "anonymous_human", referrer
        assert ev["bot_name"] is None, referrer


def test_googlebot_with_referrer_is_bot_and_excluded_from_referrers(secret):
    """R5 行为契约：Googlebot + 任意 Referer → suspected_bot，且被外部
    来源榜排除（含爬虫对照口径已随 R4 移除）；站内跨入口 Referer 按现有
    规则归 direct。Google/Baidu 真人点击不受影响（见上一用例）。"""
    bot = _ev(remote_addr="216.239.32.10",
              user_agent=_ua("googlebot"),
              referrer="https://news.example.com/from-google")
    assert bot["visitor_kind"] == "suspected_bot"
    assert bot["bot_name"] == "Googlebot"
    assert bot["referrer_domain"] == "news.example.com"  # 事件保留来源明细
    human = _ev(remote_addr="198.51.100.5", path="/demo",
                referrer="https://news.example.com/human-click")
    _flush([bot, human])
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert "top_referrers_with_bots" not in stats   # 对照口径已移除
    assert stats["top_referrers"] == [
        {"domain": "news.example.com", "visits": 1}]  # 只剩人类那条
    # 站内跨入口来源按现有规则归 direct（两入口互跳不当外部来源）
    cross = _ev(remote_addr="203.0.113.77", path="/login",
                host="pt.solarise94.fun", referrer="https://histopilot.com/")
    assert cross["referrer_domain"] == "direct"


# --------------------------------------------------------------------------- #
# 3. 日轮换哈希与 IP 前缀
# --------------------------------------------------------------------------- #
def test_ipv4_same_slash24_same_hash(secret):
    a = _ev(remote_addr="203.0.113.1")
    b = _ev(remote_addr="203.0.113.99")
    c = _ev(remote_addr="203.0.114.1")
    assert a["daily_visitor_hash"] == b["daily_visitor_hash"]
    assert a["daily_visitor_hash"] != c["daily_visitor_hash"]


def test_ipv6_same_slash64_same_hash(secret):
    a = _ev(remote_addr="2001:db8:1:2:3:4:5:6")
    b = _ev(remote_addr="2001:db8:1:2::9")
    c = _ev(remote_addr="2001:db8:1:3::1")
    assert a["daily_visitor_hash"] == b["daily_visitor_hash"]
    assert a["daily_visitor_hash"] != c["daily_visitor_hash"]


def test_ipv4_mapped_ipv6_equivalent(secret):
    a = _ev(remote_addr="::ffff:203.0.113.7")
    b = _ev(remote_addr="203.0.113.9")
    assert a["daily_visitor_hash"] == b["daily_visitor_hash"]


def test_day_rotation_and_shanghai_day_boundary(secret):
    day1 = _ev(remote_addr="203.0.113.1", now=BASE_TIME)
    day2 = _ev(remote_addr="203.0.113.1",
               now=BASE_TIME + timedelta(days=1))
    assert day1["daily_visitor_hash"] != day2["daily_visitor_hash"]
    # Asia/Shanghai 日界 = 16:00 UTC：15:59:59 与次日 00:00（本地）分属两日
    before = _ev(remote_addr="203.0.113.1",
                 now=datetime(2026, 9, 3, 15, 59, 59, tzinfo=UTC))
    after = _ev(remote_addr="203.0.113.1",
                now=datetime(2026, 9, 3, 16, 0, 0, tzinfo=UTC))
    assert before["daily_visitor_hash"] != after["daily_visitor_hash"]
    assert _ev(now=datetime(2026, 9, 3, 15, 59, 59, tzinfo=UTC))[
        "daily_visitor_hash"] == day1["daily_visitor_hash"]


def test_missing_or_unparseable_ip_drops_event(secret):
    for addr in (None, "", "   ", "not-an-ip", "999.1.1.1", "203.0.113"):
        assert _ev(remote_addr=addr) is None, repr(addr)


def test_secret_not_reused_from_session_secret(monkeypatch, tmp_path):
    # 明确设置 SECRET_KEY（session secret），但未配置采集 secret 文件 →
    # 必须停止采集（绝不能回退复用 session secret）
    monkeypatch.setenv("SECRET_KEY", "session-secret-do-not-reuse")
    assert _ev() is None
    # 采集 secret 文件存在但权限过宽同样拒绝
    _make_secret(tmp_path, monkeypatch, mode=0o644)
    assert _ev() is None
    os.chmod(str(tmp_path / "site_stats.secret"), 0o600)
    assert _ev() is not None


# --------------------------------------------------------------------------- #
# 4. secret 降级：不落事件、不抛、有告警
# --------------------------------------------------------------------------- #
def test_secret_missing_degrades_without_raise(monkeypatch, caplog):
    monkeypatch.setenv("SITE_STATS_ENTRY_HOSTS", ENTRY_HOSTS_DEFAULT)
    monkeypatch.delenv("SITE_STATS_HMAC_SECRET_FILE", raising=False)
    with caplog.at_level(logging.WARNING, logger="svs.site_stats"):
        assert _ev() is None                       # build_event 降级
        assert sss.enqueue_visit(_fake_event()) is False
        assert sss.enqueue_visit(None) is False    # 永不抛
    assert "site_stats" in caplog.text


def test_secret_bad_permissions_or_missing_file_degrades(
        tmp_path, monkeypatch):
    sss._reset_warn_state()
    # 0644 / 0640 / 0666：权限过宽 → 拒绝
    for mode in (0o644, 0o640, 0o666):
        _make_secret(tmp_path, monkeypatch, mode=mode)
        assert _ev() is None, oct(mode)
    # 文件缺失 / 空文件 / 目录路径
    monkeypatch.setenv("SITE_STATS_HMAC_SECRET_FILE",
                       str(tmp_path / "no-such.secret"))
    assert _ev() is None
    _make_secret(tmp_path, monkeypatch, content=b"")
    assert _ev() is None
    monkeypatch.setenv("SITE_STATS_HMAC_SECRET_FILE", str(tmp_path))
    assert _ev() is None


def test_secret_readable_event_built(secret):
    ev = _ev()
    assert ev is not None
    assert len(ev["daily_visitor_hash"]) == 64


# --------------------------------------------------------------------------- #
# 5. 迁移 0030 + 0053（真实 PG）
# --------------------------------------------------------------------------- #
def test_migration_0030_applied_idempotent_and_minimal():
    import pg_store
    # ensure_schema 按 tuple 行访问（row[0]），必须用默认 row_factory 连接
    plain = pg_store.connect()
    try:
        # ensure_schema 幂等：再跑两遍不报错且 0030/0053 已记录
        files = pg_store.ensure_schema(plain)
        assert "0030_site_visit_events.sql" in files
        assert "0053_site_visit_request_host.sql" in files
        files2 = pg_store.ensure_schema(plain)
        assert files == files2
    finally:
        plain.close()
    cols = _sql_all(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name='site_visit_events'"
        " ORDER BY ordinal_position")
    col_names = [r["column_name"] for r in cols]
    assert col_names == SITE_COLUMNS
    # 禁止出现原始 IP/UA/query/token/资源 ID/用户外键形态的列
    forbidden = ("ip", "agent", "query", "token", "invite", "session",
                 "user_id", "url", "raw")
    for name in col_names:
        for word in forbidden:
            assert word not in name, name
    unique = _sql_all(
        "SELECT conname FROM pg_constraint WHERE conrelid ="
        " 'site_visit_events'::regclass AND contype = 'u'")
    assert {r["conname"] for r in unique} == {
        "site_visit_events_dedup_v2_unique"}
    check = _sql_one(
        "SELECT count(*) AS n FROM pg_constraint WHERE conrelid ="
        " 'site_visit_events'::regclass AND contype = 'c'"
        " AND conname = 'site_visit_events_kind_check'")
    assert check["n"] == 1
    # 迁移文件直接重放幂等（IF NOT EXISTS / ON CONFLICT DO NOTHING）
    sql = (pg_store.migrations_dir()
           / "0030_site_visit_events.sql").read_text(encoding="utf-8")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()


def test_migration_0053_request_host_idempotent():
    """0053：request_host 列 + CHECK（NULL=历史行目标域名未知）+ 索引，
    重放幂等；request_host 为 NULL 的历史形态仍可写入（隔离口径依赖）。"""
    import pg_store
    sql = (pg_store.migrations_dir()
           / "0053_site_visit_request_host.sql").read_text(encoding="utf-8")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)      # 重放：不抛、不改行
            cur.execute(sql)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM pg_constraint WHERE conrelid ="
                " 'site_visit_events'::regclass AND contype = 'c'"
                " AND conname = 'site_visit_events_request_host_check'")
            assert cur.fetchone()["n"] == 1
            cur.execute(
                "SELECT count(*) AS n FROM pg_indexes WHERE tablename ="
                " 'site_visit_events'"
                " AND indexname = 'idx_site_visit_events_host_time'")
            assert cur.fetchone()["n"] == 1
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 6. 去重桶与落库形态（真实 PG）
# --------------------------------------------------------------------------- #
def test_dedup_bucket_unique_and_cross_bucket_new_row(secret):
    ev = _ev()
    _flush([ev, _ev()])                      # 同 hash+page+桶 → 冲突丢弃
    assert _site_count() == 1
    _flush([_ev(now=BASE_TIME + timedelta(seconds=601))])   # 跨桶 → 新行
    assert _site_count() == 2
    _flush([_ev(path="/demo")])              # 同 hash 同桶不同 page → 新行
    assert _site_count() == 3


def test_crawler_does_not_suppress_browser_visit(secret):
    _flush([_ev(user_agent='curl/8.0'), _ev()])
    assert _site_count() == 2


def test_each_entry_domain_keeps_its_visit(secret):
    _flush([_ev(host='histopilot.com'), _ev(host='pt.solarise94.fun')])
    assert _site_count() == 2


def test_converter_page_is_observable(secret):
    assert _ev(path='/tools/slides') is not None


def test_public_html_map_is_covered_and_metadata_is_excluded(secret):
    from public_site import public_pages, ACCOUNT_PAGES, DISCOVERY_PATHS
    for page in (*public_pages(), *ACCOUNT_PAGES):
        assert _ev(path=page.path) is not None, page.path
    for path in (*DISCOVERY_PATHS, '/guide.md', '/guide/conversion.md',
                 '/legal/unknown/version', '/tools/slides/private-file'):
        assert _ev(path=path) is None, path


def test_dedup_dimensions_remain_idempotent_and_visitor_metric_is_not_inflated(secret):
    events = [_ev(), _ev(signed_in=True), _ev(host='pt.solarise94.fun'),
              _ev(user_agent='Googlebot/2.1')]
    _flush(events + events)
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert _site_count() == 4
    assert stats['today'] == {'visits': 4, 'unique_visitors': 1, 'bots': 1}


def test_migration_0083_upgrades_existing_events_without_rewriting(secret):
    import pg_store
    _flush([_ev()])
    before = _sql_all('SELECT * FROM site_visit_events')
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute('ALTER TABLE site_visit_events DROP CONSTRAINT site_visit_events_dedup_v2_unique')
            cur.execute('ALTER TABLE site_visit_events ADD CONSTRAINT site_visit_events_dedup_unique '
                        'UNIQUE (daily_visitor_hash, page_key, dedup_bucket)')
            sql = (pg_store.migrations_dir() / '0083_site_visit_dedup_dimensions.sql').read_text()
            cur.execute(sql)
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()
    assert _sql_all('SELECT * FROM site_visit_events') == before
    _flush([_ev(user_agent='curl/8.0'), _ev(host='pt.solarise94.fun')])
    assert _site_count() == 3


def test_persisted_row_minimized_shape(secret):
    ev = _ev(query_string="?utm_source=n", referrer="https://news.example.com",
             user_agent="curl/8.4.0", signed_in=True)
    _flush([ev])
    row = _sql_one("SELECT * FROM site_visit_events")
    assert row is not None
    assert row["event_id"].startswith("sve_")
    assert len(row["event_id"]) == 4 + 24
    assert row["page_key"] == "home"
    assert row["request_host"] == "histopilot.com"
    assert row["referrer_domain"] == "news.example.com"
    assert row["utm_source"] == "n"
    assert row["country_code"] == "unknown"
    assert row["daily_visitor_hash"] == ev["daily_visitor_hash"]
    assert row["visitor_kind"] == "suspected_bot"
    assert row["bot_name"] == "curl"
    assert row["occurred_at"] == BASE_TIME
    assert row["expires_at"] == BASE_TIME + timedelta(
        days=sss.RETENTION_DAYS)
    assert row["dedup_bucket"] == ev["dedup_bucket"]
    # 行内任何值都不得携带原始 IP / UA / query / token 字样
    for value in row.values():
        text = str(value)
        for raw in ("203.0.113.45", "curl/8.4.0", "utm_source",
                    "share_token", "news.example.com/a"):
            assert raw not in text, (raw, text)


# --------------------------------------------------------------------------- #
# 7. retention 清理（真实 PG）
# --------------------------------------------------------------------------- #
def test_purge_expired_deletes_only_expired_rows(secret):
    old = _ev(now=BASE_TIME - timedelta(days=100))     # 到期 10 天前
    fresh = _ev()
    _flush([old, fresh])
    assert _site_count() == 2
    deleted = sss.purge_expired(now=BASE_TIME)
    assert deleted == 1
    assert _site_count() == 1
    row = _sql_one("SELECT occurred_at FROM site_visit_events")
    assert row["occurred_at"] == BASE_TIME
    # 幂等：再跑删 0 行；边界（expires_at == now）也删
    assert sss.purge_expired(now=BASE_TIME) == 0
    boundary = _ev(now=BASE_TIME - timedelta(days=sss.RETENTION_DAYS))
    _flush([boundary])
    assert sss.purge_expired(now=BASE_TIME) == 1


# --------------------------------------------------------------------------- #
# 8. dashboard_stats：形状 / 三分类互斥 / 只读
# --------------------------------------------------------------------------- #
def test_dashboard_stats_counts_and_readonly(secret):
    anon = _ev(remote_addr="203.0.113.1")
    # 同 /24 同日 → 同哈希；不同 page 落新行，但去重计数仍只算 1 个访客
    anon_same_prefix = _ev(remote_addr="203.0.113.2", path="/register")
    signed = _ev(remote_addr="198.51.100.5", path="/demo", signed_in=True,
                 referrer="https://news.example.com/a")
    bot = _ev(remote_addr="216.239.32.10",
              user_agent="Mozilla/5.0 (compatible; Googlebot/2.1)",
              referrer="https://news.example.com/b")
    _flush([anon, anon_same_prefix, signed, bot])
    before = _business_counts()
    assert before["site_visit_events"] == 4

    stats = sss.dashboard_stats(now=BASE_TIME)

    # 形状逐键一致（钉死契约）
    assert set(stats.keys()) == DASHBOARD_KEYS
    for key in ("today", "d7", "d30"):
        assert set(stats[key].keys()) == WINDOW_KEYS, key
    assert set(stats["visitor_kinds"].keys()) == set(sss.VISITOR_KINDS)
    # R4（2026-09-19）：daily 趋势 = 近 7 天共 7 行（UTC+8 日界、缺日补 0）
    assert len(stats["daily"]) == 7
    for row in stats["daily"]:
        assert set(row.keys()) == DAILY_KEYS
    assert len(stats["recent"]) == 4
    for row in stats["recent"]:
        assert set(row.keys()) == RECENT_KEYS
    for row in stats["top_referrers"]:
        assert set(row.keys()) == {"domain", "visits"}
    for row in stats["top_pages"]:
        assert set(row.keys()) == {"page_key", "visits"}
    for row in stats["top_countries"]:
        assert set(row.keys()) == {"country_code", "visits"}
    # 入口白名单可见性 + 历史行隔离键
    assert stats["entry_hosts"] == ["histopilot.com", "pt.solarise94.fun"]
    assert stats["host_filter_configured"] is True
    assert stats["legacy"] == {"d30_visits": 0}

    # 三分类互不混入：visits=事件数；bots 单列不进入 unique_visitors；
    # unique_visitors=人类日去重哈希数（同 /24 同日的两个 IP 只算 1 个）
    assert stats["today"] == {"visits": 4, "unique_visitors": 2, "bots": 1}
    assert stats["d7"] == stats["today"]
    assert stats["d30"] == stats["today"]
    assert stats["visitor_kinds"] == {
        "anonymous_human": 2, "signed_in_human": 1, "suspected_bot": 1}
    # 倒序契约第 1 行 = 今天（BASE_DAY）
    assert stats["daily"][0] == {
        "date": BASE_DAY, "visits": 4, "unique_visitors": 2, "bots": 1}
    # top：外部来源排除 direct；固定排除疑似爬虫（R4 2026-09-19 爬虫开关
    # 退役，含爬虫对照口径 top_referrers_with_bots 已移除）；国家全 unknown
    # → 空列表
    assert stats["top_referrers"] == [
        {"domain": "news.example.com", "visits": 1}]
    assert stats["top_pages"] == [
        {"page_key": "home", "visits": 2},
        {"page_key": "demo", "visits": 1},
        {"page_key": "register", "visits": 1}]
    assert stats["top_countries"] == []
    assert stats["geo_configured"] is False
    assert stats["geo_source"] is None
    # recent 按时间倒序，且逐行携带访问域名
    occurred = [r["occurred_at"] for r in stats["recent"]]
    assert occurred == sorted(occurred, reverse=True)
    assert all(r["request_host"] == "histopilot.com" for r in stats["recent"])

    # 只读：业务表与 site 表行数都不变；也不触发清理
    assert _business_counts() == before


def test_dashboard_restricts_entry_hosts_and_isolates_legacy(secret):
    """聚合口径（review 2026-09-15）：只计 request_host 命中白名单的行。

    - 同机其它域名的历史混入行（request_host=cpa.ni-biolab.com）：被新采集
      拒之门外，此处手工落一行模拟迁移前已混入的存量——主口径必须排除；
    - 迁移前历史行（request_host IS NULL = 目标域名未知）：排除出主口径，
      仅 legacy.d30_visits 计数，不删除。"""
    ev = _ev()                                     # histopilot.com，白名单内
    ev2 = _ev(remote_addr="198.51.100.9", path="/login",
              host="pt.solarise94.fun")            # 第二入口，也在白名单内
    _flush([ev, ev2])
    # 手工落两类"混入/历史"行（绕开 build_event 准入，模拟存量数据）
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO site_visit_events (event_id, occurred_at,"
                " expires_at, dedup_bucket, page_key, request_host,"
                " referrer_domain, country_code, daily_visitor_hash,"
                " visitor_kind) VALUES"
                " ('sve_foreign0000000000000001', %s, %s, %s, 'home',"
                "  'cpa.ni-biolab.com', 'direct', 'unknown', %s,"
                "  'anonymous_human'),"
                " ('sve_legacy00000000000000001', %s, %s, %s, 'home',"
                "  NULL, 'direct', 'unknown', %s, 'anonymous_human')",
                (BASE_TIME, BASE_TIME + timedelta(days=90),
                 int(BASE_TIME.timestamp() // 600), "b" * 64,
                 BASE_TIME, BASE_TIME + timedelta(days=90),
                 int(BASE_TIME.timestamp() // 600) + 1, "c" * 64))
        conn.commit()
    finally:
        conn.close()
    assert _site_count() == 4

    stats = sss.dashboard_stats(now=BASE_TIME)
    # 主口径只认两个入口域名：窗口/趋势/分类/Top/recent 全部不含混入行
    assert stats["today"] == {"visits": 2, "unique_visitors": 2, "bots": 0}
    assert stats["visitor_kinds"] == {
        "anonymous_human": 2, "signed_in_human": 0, "suspected_bot": 0}
    assert stats["top_pages"] == [
        {"page_key": "home", "visits": 1}, {"page_key": "login", "visits": 1}]
    recent_hosts = sorted(r["request_host"] for r in stats["recent"])
    assert recent_hosts == ["histopilot.com", "pt.solarise94.fun"]
    # 历史行单独计数（不进入主口径，也不被删除）
    assert stats["legacy"] == {"d30_visits": 1}
    assert _site_count() == 4
    # recent 中的访问域名可分辨两个入口
    by_page = {r["page_key"]: r["request_host"] for r in stats["recent"]}
    assert by_page["home"] == "histopilot.com"
    assert by_page["login"] == "pt.solarise94.fun"


def test_top_referrers_exclude_ip_literal_history_rows(secret, monkeypatch):
    """外部来源榜排除 IP 字面量 referrer（历史行不改写，只在聚合/展示层排除）。

    生产实例（2026-10-16）：扫描器直访 tencentfast 裸 IP 后带
    ``Referer: https://43.142.130.199/`` 落库 referrer_domain=
    '43.142.130.199'（histopilot.cn，13 行）。采集口径修正不回写历史行
    （不做数据迁移）；top_referrers 聚合固定排除 IPv4 四段数字与含冒号
    （IPv6）形态，使存量行立即从来源榜消失；recent 明细保留行本身但
    referrer_domain 读取口径映射为 direct；真实外部来源不受影响。"""
    # 生产实例的入口是 histopilot.cn（当时在白名单内）——纳入白名单，
    # 使历史行进入主口径，证明排除靠的是 referrer 规则而非 host 隔离
    monkeypatch.setenv("SITE_STATS_ENTRY_HOSTS",
                       ENTRY_HOSTS_DEFAULT + ",histopilot.cn")
    human = _ev(remote_addr="198.51.100.5", path="/demo",
                referrer="https://news.example.com/real")
    _flush([human])
    # 手工落历史形态行（绕开 build_event，模拟口径修正前的存量数据）；
    # 3 条 IP 行若不排除，合计将反超唯一的真实来源，证明排除与排序无关
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO site_visit_events (event_id, occurred_at,"
                " expires_at, dedup_bucket, page_key, request_host,"
                " referrer_domain, country_code, daily_visitor_hash,"
                " visitor_kind) VALUES"
                # 生产实例同款：IPv4 字面量（扫描器）
                " ('sve_iphist0000000000000001', %s, %s, %s, 'home',"
                "  'histopilot.cn', '43.142.130.199', 'unknown', %s,"
                "  'anonymous_human'),"
                # IPv6 字面量（含冒号）
                " ('sve_iphist0000000000000002', %s, %s, %s, 'home',"
                "  'histopilot.cn', '::1', 'unknown', %s,"
                "  'anonymous_human'),"
                # IPv4-mapped 形态（同样含冒号）
                " ('sve_iphist0000000000000003', %s, %s, %s, 'home',"
                "  'histopilot.cn', '::ffff:203.0.113.9', 'unknown', %s,"
                "  'anonymous_human')",
                (BASE_TIME, BASE_TIME + timedelta(days=90),
                 int(BASE_TIME.timestamp() // 600) + 0, "d" * 64,
                 BASE_TIME, BASE_TIME + timedelta(days=90),
                 int(BASE_TIME.timestamp() // 600) + 1, "e" * 64,
                 BASE_TIME, BASE_TIME + timedelta(days=90),
                 int(BASE_TIME.timestamp() // 600) + 2, "f" * 64))
        conn.commit()
    finally:
        conn.close()
    assert _site_count() == 4

    stats = sss.dashboard_stats(now=BASE_TIME)
    # 来源榜只剩真实外部来源；IP 字面量历史行（3 条）不出现
    assert stats["top_referrers"] == [
        {"domain": "news.example.com", "visits": 1}]
    # 总览计数是「访问量」口径，与 referrer 无关：历史行照常计入
    assert stats["today"]["visits"] == 4
    # recent 明细不隐藏行（4 条都在），但 IP 字面量 referrer 读取口径
    # 映射为 direct，真实来源原样保留
    assert len(stats["recent"]) == 4
    by_hash = {r["referrer_domain"]: r for r in stats["recent"]
               if r["referrer_domain"] != "direct"}
    assert set(by_hash) == {"news.example.com"}
    # 历史行不改写：IP 字面量仍原样在库（只删自己造的数据由夹具负责）
    counts = _sql_all(
        "SELECT referrer_domain, count(*) AS n FROM site_visit_events"
        " WHERE position(':' in referrer_domain) > 0"
        " OR referrer_domain ~ '^[0-9]+(\\.[0-9]+){3}$'"
        " GROUP BY 1 ORDER BY 1")
    assert {(r["referrer_domain"], r["n"]) for r in counts} == {
        ("43.142.130.199", 1), ("::1", 1), ("::ffff:203.0.113.9", 1)}


def test_top_referrers_exclude_internal_referrer_history_rows(
        secret, monkeypatch):
    """外部来源榜排除自有中转域名的历史存量行（review 2026-10-18）。

    生产事实：近 30 天 13 行 referrer_domain=goapi.solarise94.fun
    （request_host=histopilot.cn，公开 HTML GET，均未命中 bot 规则）。采集
    口径修正不回写历史行（不做数据迁移/删除/改判 bot）：top_referrers 经
    ``<> ALL(%s)`` 参数化排除当前自有/内部集合，recent 明细保留行本身、
    referrer_domain 读取口径映射为 direct——与裸 IP 字面量历史行同构。
    精确 hostname 匹配：未列出的其它子域照常保留，不做后缀过滤。"""
    monkeypatch.setenv("COS_FAST_RELAY_URL", "https://goapi.solarise94.fun")
    # 生产实例的入口是 histopilot.cn——纳入白名单，使历史行进入主口径，
    # 证明排除靠的是 referrer 规则而非 host 隔离
    monkeypatch.setenv("SITE_STATS_ENTRY_HOSTS",
                       ENTRY_HOSTS_DEFAULT + ",histopilot.cn")
    human = _ev(remote_addr="198.51.100.5", path="/register",
                referrer="https://www.google.com/search?q=histopilot")
    _flush([human])
    # 手工落历史形态行（绕开 build_event，模拟口径修正前的存量数据）：
    # goapi 3 条（生产 13 条的代理）+ 未列出子域 1 条（不得被误伤）
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO site_visit_events (event_id, occurred_at,"
                " expires_at, dedup_bucket, page_key, request_host,"
                " referrer_domain, country_code, daily_visitor_hash,"
                " visitor_kind) VALUES"
                " ('sve_relayhist000000000000001', %s, %s, %s, 'home',"
                "  'histopilot.cn', 'goapi.solarise94.fun', 'unknown', %s,"
                "  'anonymous_human'),"
                " ('sve_relayhist000000000000002', %s, %s, %s, 'guide',"
                "  'histopilot.cn', 'goapi.solarise94.fun', 'unknown', %s,"
                "  'anonymous_human'),"
                " ('sve_relayhist000000000000003', %s, %s, %s, 'login',"
                "  'histopilot.cn', 'goapi.solarise94.fun', 'unknown', %s,"
                "  'anonymous_human'),"
                " ('sve_relayhist000000000000004', %s, %s, %s, 'demo',"
                "  'histopilot.cn', 'other.solarise94.fun', 'unknown', %s,"
                "  'anonymous_human')",
                (BASE_TIME, BASE_TIME + timedelta(days=90),
                 int(BASE_TIME.timestamp() // 600) + 0, "d" * 64,
                 BASE_TIME, BASE_TIME + timedelta(days=90),
                 int(BASE_TIME.timestamp() // 600) + 1, "e" * 64,
                 BASE_TIME, BASE_TIME + timedelta(days=90),
                 int(BASE_TIME.timestamp() // 600) + 2, "f" * 64,
                 BASE_TIME, BASE_TIME + timedelta(days=90),
                 int(BASE_TIME.timestamp() // 600) + 3, "6" * 64))
        conn.commit()
    finally:
        conn.close()
    assert _site_count() == 5

    stats = sss.dashboard_stats(now=BASE_TIME)
    # 来源榜：goapi 历史行（3 条）全部消失；未列出子域与真实外部来源照常
    # 保留（同计数按 value 升序）
    assert stats["top_referrers"] == [
        {"domain": "other.solarise94.fun", "visits": 1},
        {"domain": "www.google.com", "visits": 1}]
    # 总览是「访问量」口径，与 referrer 无关：历史行照常计入、不删除、
    # 不改判 bot（仍是 anonymous_human）
    assert stats["today"]["visits"] == 5
    assert stats["visitor_kinds"]["anonymous_human"] == 5
    assert stats["visitor_kinds"]["suspected_bot"] == 0
    # recent 明细保留全部行；goapi 行读取口径映射 direct，未列出子域原样
    assert len(stats["recent"]) == 5
    by_page = {r["page_key"]: r["referrer_domain"] for r in stats["recent"]}
    assert by_page["home"] == "direct"
    assert by_page["guide"] == "direct"
    assert by_page["login"] == "direct"
    assert by_page["demo"] == "other.solarise94.fun"
    assert by_page["register"] == "www.google.com"
    # 历史行不改写：goapi 域名仍原样在库
    counts = _sql_all(
        "SELECT referrer_domain, count(*) AS n FROM site_visit_events"
        " WHERE position('solarise94.fun' in referrer_domain) > 0"
        " GROUP BY 1 ORDER BY 1")
    assert {(r["referrer_domain"], r["n"]) for r in counts} == {
        ("goapi.solarise94.fun", 3), ("other.solarise94.fun", 1)}


def test_dashboard_relay_exclusion_follows_live_config(secret, monkeypatch):
    """排除集合每次调用重读 env：同一批历史行，配置生效前照常上榜（无法
    认定自有），配置后无需任何数据改写即从榜上消失、recent 同步映射
    direct——口径切换只取决于当前配置，存量数据零迁移。"""
    monkeypatch.delenv("COS_FAST_RELAY_URL", raising=False)
    monkeypatch.delenv("SITE_STATS_INTERNAL_REFERRER_HOSTS", raising=False)
    _flush([_ev(remote_addr="198.51.100.5", referrer="https://news.example.com"),
            _ev(remote_addr="198.51.100.6", path="/demo",
                referrer="https://goapi.solarise94.fun/x")])
    stats = sss.dashboard_stats(now=BASE_TIME)
    # 未配置中转域名 → 无法认定自有：goapi 历史行照常上榜（旧口径原样，
    # 不崩溃也不误伤）
    assert stats["top_referrers"] == [
        {"domain": "goapi.solarise94.fun", "visits": 1},
        {"domain": "news.example.com", "visits": 1}]
    # 配置生效（含畸形值 → 空集，同样不排除）后同一批行立即消失；
    # COS_FAST_RELAY_URL 非法/为空 → 不崩溃、恢复旧口径（nothing excluded）
    monkeypatch.setenv("COS_FAST_RELAY_URL", "https://goapi.solarise94.fun")
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert stats["top_referrers"] == [
        {"domain": "news.example.com", "visits": 1}]
    assert [r["referrer_domain"] for r in stats["recent"]
            if r["page_key"] == "demo"] == ["direct"]
    monkeypatch.setenv("COS_FAST_RELAY_URL", "::::")
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert {"domain": "goapi.solarise94.fun", "visits": 1} in \
        stats["top_referrers"]


def test_dashboard_fail_closed_when_allowlist_unconfigured(
        monkeypatch, secret):
    """白名单未配置：主口径恒空 + host_filter_configured=False（面板亮
    提示），legacy 计数仍可见；已有白名单行不被计入。"""
    _flush([_ev()])
    monkeypatch.delenv("SITE_STATS_ENTRY_HOSTS", raising=False)
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert stats["host_filter_configured"] is False
    assert stats["entry_hosts"] == []
    assert stats["today"] == {"visits": 0, "unique_visitors": 0, "bots": 0}
    assert stats["d30"]["visits"] == 0
    assert stats["recent"] == []
    assert stats["top_pages"] == []
    # 白名单行不是"历史行"：legacy 只数 request_host IS NULL
    assert stats["legacy"] == {"d30_visits": 0}


def test_dashboard_stats_daily_series_is_seven_days_descending(secret):
    """R4（review 2026-09-19）：daily 趋势 = 近 7 天共 7 行、严格倒序
    （第 1 行 = 今天）、缺日补 0；today/d7/d30 KPI 窗口不变——7 天窗口外、
    30 天窗口内的旧事件仍计入 d30 KPI（30 天 KPI 没有被误切成 7 天）。"""
    # 不同 /24 避免去重互相吞并；today 与各历史日各 1 条
    _flush([
        _ev(remote_addr="203.0.113.1"),                                      # 今天
        _ev(remote_addr="203.0.114.2",
            now=BASE_TIME - timedelta(days=3)),                             # 3 天前
        _ev(remote_addr="203.0.115.3",
            now=BASE_TIME - timedelta(days=10)),                            # 10 天前
        _ev(remote_addr="203.0.116.4",
            now=BASE_TIME - timedelta(days=29)),                            # 29 天前
        _ev(remote_addr="203.0.117.5",
            now=BASE_TIME - timedelta(days=40)),                            # 窗口外
    ])
    stats = sss.dashboard_stats(now=BASE_TIME)
    # 30 天 KPI 仍覆盖完整 30 天窗口（4 条），7 天趋势只含其中 2 条
    assert stats["d30"]["visits"] == 4
    assert stats["d7"]["visits"] == 2
    assert len(stats["daily"]) == 7
    base_day = datetime.strptime(BASE_DAY, "%Y-%m-%d").date()
    # 严格倒序：第 1 行 = 今天，随后逐日回退，最后一行 = 今天 − 6 天
    dates = [row["date"] for row in stats["daily"]]
    assert dates == [(base_day - timedelta(days=i)).isoformat()
                     for i in range(7)]
    assert all(
        dates[i] > dates[i + 1] for i in range(len(dates) - 1))
    by_date = {row["date"]: row for row in stats["daily"]}
    assert by_date[BASE_DAY]["visits"] == 1
    assert by_date[(base_day - timedelta(days=3)).isoformat()]["visits"] == 1
    # 缺日补 0：其余 5 天全零（10/29 天前的事件不进入 7 天趋势）
    assert sum(row["visits"] for row in stats["daily"]) == 2
    assert sum(1 for row in stats["daily"] if row["visits"] == 0) == 5


def test_dashboard_stats_daily_seven_rows_cross_month_year_and_utc8_boundary(
        secret):
    """R4 通过条件：冻结日期跨月/跨年 + UTC+8 零点边界下仍是共 7 行的倒序
    序列，补零正确。

    冻结 now = 2025-12-31 16:30 UTC = 本地（UTC+8）2026-01-01 00:30
    （跨年又跨月的今天）。7 行应为 2026-01-01 → 2025-12-26。
    """
    frozen_utc = datetime(2025, 12, 31, 16, 30, 0, tzinfo=UTC)
    boundary_before = datetime(2025, 12, 31, 15, 59, 59, tzinfo=UTC)
    boundary_after = datetime(2025, 12, 31, 16, 0, 0, tzinfo=UTC)
    _flush([
        # UTC+8 零点前 1 秒 → 本地 2025-12-31 23:59:59（趋势第 2 行）
        _ev(remote_addr="203.0.113.1", now=boundary_before),
        # UTC+8 零点整 → 本地 2026-01-01 00:00:00（趋势第 1 行「今天」）
        _ev(remote_addr="203.0.114.2", now=boundary_after),
        # 趋势最末行（今天 − 6 天 = 2025-12-26 本地）：UTC 02:00 = 本地 10:00
        _ev(remote_addr="203.0.115.3",
            now=datetime(2025, 12, 26, 2, 0, 0, tzinfo=UTC)),
        # 7 天窗口外（本地 2025-12-25）：不入趋势，但仍计入 d30 KPI
        _ev(remote_addr="203.0.116.4",
            now=datetime(2025, 12, 25, 2, 0, 0, tzinfo=UTC)),
    ])
    stats = sss.dashboard_stats(now=frozen_utc)
    dates = [row["date"] for row in stats["daily"]]
    assert dates == [
        "2026-01-01", "2025-12-31", "2025-12-30", "2025-12-29",
        "2025-12-28", "2025-12-27", "2025-12-26"]
    assert all(dates[i] > dates[i + 1] for i in range(len(dates) - 1))
    # 零点两侧各 1 条，分属第 1/2 行；最末行 1 条；其余 4 天补 0
    visits = {row["date"]: row["visits"] for row in stats["daily"]}
    assert visits == {"2026-01-01": 1, "2025-12-31": 1, "2025-12-26": 1,
                      "2025-12-30": 0, "2025-12-29": 0, "2025-12-28": 0,
                      "2025-12-27": 0}
    # 30 天 KPI 未被误切：窗口外那条（本地 2025-12-25）仍计入 d30
    assert stats["d30"]["visits"] == 4
    assert stats["d7"]["visits"] == 3


def test_dashboard_stats_excludes_expired_window_and_never_purges(secret):
    outside = _ev(now=BASE_TIME - timedelta(days=40))
    fresh = _ev()
    _flush([outside, fresh])
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert stats["today"]["visits"] == 1
    assert stats["d30"]["visits"] == 1
    assert stats["d30"]["unique_visitors"] == 1
    # 7 天趋势里任何一天都不计入 40 天前的事件
    assert all(row["visits"] <= 1 for row in stats["daily"])
    assert sum(row["visits"] for row in stats["daily"]) == 1
    # 只读聚合不得顺带清理到期行（outside 行 expires_at 未到本例 now，但
    # 用一个已到期行专门验证「不清理」）
    expired = _ev(now=BASE_TIME - timedelta(days=100))
    _flush([expired])
    stats2 = sss.dashboard_stats(now=BASE_TIME)
    assert _site_count() == 3            # 旧行仍在：stats 不调清理
    assert stats2["today"]["visits"] == 1
    assert sss.purge_expired(now=BASE_TIME) == 1   # 显式清理才删


# --------------------------------------------------------------------------- #
# 8b. 离线 IP→国家码（2026-11-08，DB-IP Lite MMDB；测试不提交真实库文件）
# --------------------------------------------------------------------------- #
class _FakeGeoReader:
    """最小 maxminddb Reader 替身：``.get(ip)`` + ``.metadata()``。"""

    def __init__(self, records=None, build_epoch=1700000000):
        self.records = dict(records or {})
        self.build_epoch = build_epoch
        self.calls = []
        self.closed = False

    def get(self, ip):
        self.calls.append(ip)
        value = self.records.get(ip)
        if isinstance(value, BaseException):
            raise value
        return value

    def metadata(self):
        return types.SimpleNamespace(build_epoch=self.build_epoch)

    def close(self):  # pragma: no cover - 仅用于断言不会被显式调用
        self.closed = True


def _use_geo(monkeypatch, reader):
    monkeypatch.setattr(sss, "_open_geo_database", lambda _path: reader)


@pytest.fixture
def geo_db(tmp_path, monkeypatch):
    """假 MMDB 文件 + env；真实打开动作由 ``_open_geo_database`` 桩替代。"""
    path = tmp_path / "dbip-country-lite-test.mmdb"
    path.write_bytes(b"fake-mmdb-v1")
    monkeypatch.setenv("SITE_STATS_GEOIP_DB", str(path))
    sss._reset_geo_state()
    return path


def test_geo_disabled_env_unset_keeps_unknown_and_no_source(secret):
    """未配置 SITE_STATS_GEOIP_DB：与 D2 首发行为完全一致。"""
    ev = _ev(remote_addr="8.8.8.8")
    assert ev["country_code"] == "unknown"
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert stats["geo_configured"] is False
    assert stats["geo_source"] is None


def test_geo_public_ipv4_ipv6_and_ipv4_mapped(secret, geo_db, monkeypatch):
    reader = _FakeGeoReader({
        "8.8.8.8": {"country": {"iso_code": "US"}},
        "2001:4860:4860::8888": {"country": {"iso_code": "JP"}},
    })
    _use_geo(monkeypatch, reader)
    assert _ev(remote_addr="8.8.8.8")["country_code"] == "US"
    assert _ev(remote_addr="2001:4860:4860::8888")["country_code"] == "JP"
    ev = _ev(remote_addr="::ffff:8.8.8.8")
    assert ev["country_code"] == "US"
    # IPv4-mapped 归一到 IPv4 查询键；原始 IP 不进入事件
    assert reader.calls[-1] == "8.8.8.8"
    assert "8.8.8.8" not in json.dumps(ev)


def test_geo_non_public_addresses_unknown_without_lookup(secret, geo_db,
                                                        monkeypatch):
    reader = _FakeGeoReader({"8.8.8.8": {"country": {"iso_code": "US"}}})
    _use_geo(monkeypatch, reader)
    for ip in ("10.1.2.3", "172.16.0.9", "192.168.1.1", "127.0.0.1",
               "169.254.1.1", "224.0.0.1", "240.0.0.1", "0.0.0.0",
               "::1", "fe80::1", "ff02::1", "::"):
        assert _ev(remote_addr=ip)["country_code"] == "unknown", ip
    assert reader.calls == []                 # 内网/保留地址不触发库查询


def test_geo_unparseable_or_empty_ip_unknown(secret, geo_db, monkeypatch):
    reader = _FakeGeoReader({"8.8.8.8": {"country": {"iso_code": "US"}}})
    _use_geo(monkeypatch, reader)
    assert sss._country_for_ip("not-an-ip") == "unknown"
    assert sss._country_for_ip("") == "unknown"
    assert sss._country_for_ip(None) == "unknown"
    assert reader.calls == []


def test_geo_missing_country_or_malformed_iso_unknown(secret, geo_db,
                                                      monkeypatch):
    """record 只有 continent / registered_country 或 iso_code 形态非法：
    一律 unknown（**不**回退其它字段）。"""
    reader = _FakeGeoReader({"8.8.8.8": {}})
    _use_geo(monkeypatch, reader)
    bad_records = (
        {},                                        # 无记录内容
        {"continent": {"code": "NA"}},             # 只有大洲
        {"registered_country": {"iso_code": "US"}},  # 只有注册国
        {"country": None},
        {"country": {}},
        {"country": {"iso_code": "us"}},           # 小写
        {"country": {"iso_code": "USA"}},          # 三位
        {"country": {"iso_code": "U1"}},
        {"country": {"iso_code": 1}},
        {"country": {"iso_code": None}},
    )
    for record in bad_records:
        reader.records["8.8.8.8"] = record
        assert _ev(remote_addr="8.8.8.8")["country_code"] == "unknown", record


def test_geo_lookup_exception_degrades_to_unknown(secret, geo_db, monkeypatch):
    reader = _FakeGeoReader({"8.8.8.8": RuntimeError("corrupt db")})
    _use_geo(monkeypatch, reader)
    ev = _ev(remote_addr="8.8.8.8")
    assert ev is not None
    assert ev["country_code"] == "unknown"     # 单条异常不抛进请求路径
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert stats["geo_configured"] is True     # reader 本身仍可用


def test_geo_dashboard_configured_exposes_source(secret, geo_db, monkeypatch):
    reader = _FakeGeoReader({}, build_epoch=1730000000)
    _use_geo(monkeypatch, reader)
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert stats["geo_configured"] is True
    assert stats["geo_source"] == {
        "name": "DB-IP",
        "url": "https://db-ip.com",
        "license": "CC BY 4.0",
        "build_epoch": 1730000000,
    }


def test_geo_dashboard_build_epoch_none_when_metadata_unavailable(
        secret, geo_db, monkeypatch):
    class _NoMetadataReader(_FakeGeoReader):
        def metadata(self):
            raise RuntimeError("bad metadata")

    _use_geo(monkeypatch, _NoMetadataReader())
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert stats["geo_configured"] is True
    assert stats["geo_source"]["build_epoch"] is None


def test_dashboard_top_countries_aggregates_non_unknown(secret):
    """top_countries 只聚合非 unknown 国家码并排除 unknown。"""
    _flush([
        _fake_event(country_code="CN", daily_visitor_hash="1" * 64),
        _fake_event(country_code="CN", daily_visitor_hash="2" * 64),
        _fake_event(country_code="US", daily_visitor_hash="3" * 64),
        _fake_event(country_code="unknown", daily_visitor_hash="4" * 64),
    ])
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert stats["top_countries"] == [
        {"country_code": "CN", "visits": 2},
        {"country_code": "US", "visits": 1},
    ]


def test_geo_reader_reloads_when_file_signature_changes(
        secret, tmp_path, monkeypatch):
    """月度原子替换：签名 (mtime_ns, size, inode) 变化才重开；相同签名不重开。"""
    path = tmp_path / "dbip.mmdb"
    path.write_bytes(b"v1")
    monkeypatch.setenv("SITE_STATS_GEOIP_DB", str(path))
    sss._reset_geo_state()
    monkeypatch.setattr(sss, "_GEO_SIGNATURE_CHECK_SECONDS", 0.0)
    r1 = _FakeGeoReader(
        {"8.8.8.8": {"country": {"iso_code": "US"}}}, build_epoch=1)
    r2 = _FakeGeoReader(
        {"8.8.8.8": {"country": {"iso_code": "CN"}}}, build_epoch=2)
    opened = []
    readers = iter([r1, r2])

    def opener(db_path):
        opened.append(db_path)
        return next(readers)

    monkeypatch.setattr(sss, "_open_geo_database", opener)
    assert _ev(remote_addr="8.8.8.8")["country_code"] == "US"
    assert _ev(remote_addr="8.8.8.8")["country_code"] == "US"
    assert len(opened) == 1                    # 签名未变：复用 reader
    path.write_bytes(b"v2-larger")             # 模拟原子 rename 后的新库
    assert _ev(remote_addr="8.8.8.8")["country_code"] == "CN"
    assert len(opened) == 2
    assert sss.dashboard_stats(now=BASE_TIME)["geo_source"]["build_epoch"] == 2
    # 旧 reader 不被显式 close（并发读者安全；由 GC 回收）
    assert r1.closed is False


def test_geo_reopen_failure_keeps_previous_reader(
        secret, tmp_path, monkeypatch):
    path = tmp_path / "dbip.mmdb"
    path.write_bytes(b"v1")
    monkeypatch.setenv("SITE_STATS_GEOIP_DB", str(path))
    sss._reset_geo_state()
    monkeypatch.setattr(sss, "_GEO_SIGNATURE_CHECK_SECONDS", 0.0)
    good = _FakeGeoReader(
        {"8.8.8.8": {"country": {"iso_code": "US"}}})

    def opener(_db_path):
        raise OSError("new db is corrupt")

    monkeypatch.setattr(sss, "_open_geo_database", opener)
    # 第一次打开也失败 → unknown（无旧 reader 可退）
    assert _ev(remote_addr="8.8.8.8")["country_code"] == "unknown"

    monkeypatch.setattr(sss, "_open_geo_database", lambda _p: good)
    assert _ev(remote_addr="8.8.8.8")["country_code"] == "US"
    # 新库替换后重开失败：继续用旧 reader，统计不中断
    path.write_bytes(b"v2-larger")
    monkeypatch.setattr(sss, "_open_geo_database", opener)
    assert _ev(remote_addr="8.8.8.8")["country_code"] == "US"
    assert good.closed is False


def test_geo_missing_file_degrades(secret, tmp_path, monkeypatch):
    monkeypatch.setenv("SITE_STATS_GEOIP_DB",
                       str(tmp_path / "absent-country.mmdb"))
    sss._reset_geo_state()
    assert _ev(remote_addr="8.8.8.8")["country_code"] == "unknown"
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert stats["geo_configured"] is False
    assert stats["geo_source"] is None


def test_geo_reader_open_is_thread_safe(secret, geo_db, monkeypatch):
    """并发首用只打开一次（锁串行化），所有线程拿到同一结果。"""
    reader = _FakeGeoReader({"8.8.8.8": {"country": {"iso_code": "US"}}})
    opened = []

    def opener(db_path):
        opened.append(db_path)
        time.sleep(0.01)
        return reader

    monkeypatch.setattr(sss, "_open_geo_database", opener)
    results = []
    errors = []

    def worker():
        try:
            results.append(sss._country_for_ip("8.8.8.8"))
        except Exception as exc:  # pragma: no cover - 失败即测试红
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors
    assert results == ["US"] * 8
    assert len(opened) == 1


# --------------------------------------------------------------------------- #
# 9. worker / 队列降级矩阵
# --------------------------------------------------------------------------- #
def test_enqueue_without_worker_returns_false():
    assert sss.enqueue_visit(_fake_event()) is False
    assert sss.enqueue_visit(None) is False


def test_start_worker_without_storage_backend_env(monkeypatch):
    """缺 STORAGE_BACKEND 仍起 worker（Wave 3：不再把缺键当 json）。"""
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.setattr(sss, "_flush_batch", lambda batch: None)
    sss.start_worker()
    try:
        assert sss._WORKER is not None
        assert sss._WORKER["thread"].is_alive()
        assert sss.enqueue_visit(_fake_event()) is True
    finally:
        sss.stop_worker()


def test_dashboard_stats_queries_table_without_storage_backend_env(
        monkeypatch, secret):
    """缺 STORAGE_BACKEND 时 dashboard 也必须真实查表（回归：afc1210 以前
    缺键直接返回全零空形状）。"""
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    _flush([_ev(remote_addr="203.0.113.1")])
    stats = sss.dashboard_stats(now=BASE_TIME)
    assert stats["today"]["visits"] == 1
    assert stats["today"]["unique_visitors"] == 1
    assert len(stats["recent"]) == 1
    assert sss.purge_expired(now=BASE_TIME) == 0  # 未到期的行仍在


def test_start_stop_worker_idempotent(monkeypatch):
    monkeypatch.setattr(sss, "_flush_batch", lambda batch: None)
    sss.start_worker()
    thread = sss._WORKER["thread"]
    sss.start_worker()                       # 幂等：不重复起线程
    assert sss._WORKER["thread"] is thread
    sss.stop_worker()
    assert sss._WORKER is None
    sss.stop_worker()                        # 幂等：重复 stop 不抛
    sss.start_worker()                       # 可重启
    assert sss._WORKER is not None
    sss.stop_worker()


def test_enqueue_rejects_non_minimized_payloads(monkeypatch):
    monkeypatch.setattr(sss, "_flush_batch", lambda batch: None)
    sss.start_worker()
    try:
        # 队列只收 build_event 形状的最小事件——带原始 IP/UA 的 dict 形状
        # 不满足固定键集合，直接 False
        assert sss.enqueue_visit({"page_key": "home"}) is False
        assert sss.enqueue_visit({"remote_addr": "203.0.113.45",
                                  "user_agent": "x"}) is False
        assert sss.enqueue_visit(["not", "a", "dict"]) is False
    finally:
        sss.stop_worker()


def test_queue_full_returns_false_without_raise(monkeypatch):
    monkeypatch.setattr(sss, "_QUEUE_CAPACITY", 2)
    monkeypatch.setattr(sss, "_BATCH_SIZE", 1)
    release = threading.Event()
    started = threading.Event()

    def _blocked_flush(batch):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(sss, "_flush_batch", _blocked_flush)
    sss.start_worker()
    try:
        assert sss.enqueue_visit(_fake_event()) is True
        assert started.wait(timeout=5)       # 第一条已被 worker 取走（阻塞在 flush）
        assert sss.enqueue_visit(_fake_event()) is True
        assert sss.enqueue_visit(_fake_event()) is True
        assert sss.enqueue_visit(_fake_event()) is False   # 队列满：丢该条
        assert sss.enqueue_visit(None) is False
    finally:
        release.set()
        sss.stop_worker()


def test_db_failure_drops_batch_without_raise(monkeypatch, caplog):
    monkeypatch.setattr(sss, "_WARN_INTERVAL_SECONDS", 0.0)
    attempts = []

    def _boom():
        attempts.append(1)
        raise RuntimeError("db down")

    monkeypatch.setattr(sss.pg_store, "connect", _boom)
    sss.start_worker()
    try:
        with caplog.at_level(logging.WARNING, logger="svs.site_stats"):
            assert sss.enqueue_visit(_fake_event()) is True
            assert _wait_until(lambda: bool(attempts))
            assert sss.enqueue_visit(_fake_event()) is True
            assert _wait_until(lambda: len(attempts) >= 2)
        # 两次 flush 全部失败但 worker 未崩、异常未外泄、日志无事件内容
        assert sss._WORKER["thread"].is_alive()
        assert "daily_visitor_hash" not in caplog.text
        assert "203.0.113" not in caplog.text
    finally:
        sss.stop_worker()


# --------------------------------------------------------------------------- #
# 10. app.py 接线契约（并行代理实施中；默认启用，未接线时红属预期）
# --------------------------------------------------------------------------- #


def _login(client, user):
    with client.session_transaction() as s:
        s["auth_user"] = user.get("login_id") or user.get("user_id")
        s["user_id"] = user["user_id"]
        s["role"] = user.get("role") or "user"
        s["auth_version"] = user.get("auth_version", 1)
    return client


@site_stats_app_wiring
def test_app_admin_site_stats_owner_only(monkeypatch):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    owner = user_store.create_user("owner@x.test", "ownerpass1234567",
                                   role="owner", display_name="Owner")
    user = user_store.create_user("user@x.test", "userpass12345678",
                                  role="user", display_name="User")
    client = _client()

    # 匿名 401
    resp = client.get("/api/admin/v1/site-stats")
    assert resp.status_code == 401
    # user 403
    _login(client, user)
    resp = client.get("/api/admin/v1/site-stats")
    assert resp.status_code == 403
    # owner 200 且形状与钉死契约逐键一致
    _login(client, owner)
    resp = client.get("/api/admin/v1/site-stats")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert set(payload.keys()) == DASHBOARD_KEYS
    for key in ("today", "d7", "d30"):
        assert set(payload[key].keys()) == WINDOW_KEYS
    assert set(payload["visitor_kinds"].keys()) == set(sss.VISITOR_KINDS)
    # R4（2026-09-19）：API 明确提供近 7 天倒序契约
    assert len(payload["daily"]) == 7
    for row in payload["daily"]:
        assert set(row.keys()) == DAILY_KEYS
    assert payload["geo_configured"] is False
    assert payload["geo_source"] is None
    # 无写副作用：调用前后 site 表行数不变（不创建事件、不清理）
    n_before = _site_count()
    resp = client.get("/api/admin/v1/site-stats")
    assert resp.status_code == 200
    assert _site_count() == n_before


@site_stats_app_wiring
def test_app_after_request_records_public_html_get(secret, monkeypatch):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    # test client 的 Host 是 localhost：白名单按入口域名收窄后需显式放行
    monkeypatch.setenv("SITE_STATS_ENTRY_HOSTS", "localhost")
    sss.start_worker()                       # 幂等；若 app 启动已起则 no-op
    try:
        client = _client()
        resp = client.get("/")               # 公开 HTML GET → 应落 home 事件
        assert resp.status_code == 200
        assert _wait_until(lambda: _site_count() >= 1)
        row = _sql_one("SELECT page_key, request_host, visitor_kind,"
                       " country_code, referrer_domain"
                       " FROM site_visit_events")
        assert row["page_key"] == "home"
        assert row["request_host"] == "localhost"
        assert row["visitor_kind"] in sss.VISITOR_KINDS
        assert row["country_code"] == "unknown"
        assert row["referrer_domain"] == "direct"
        # 非 HTML JSON 端点不落事件
        n = _site_count()
        assert client.get("/healthz").status_code == 200
        time.sleep(1.0)
        assert _site_count() == n
    finally:
        sss.stop_worker()


# --------------------------------------------------------------------------- #
# 11. F4 + review R2-F5：每日保留任务接线（双跑纯单元）。
# R2-F5 拆分后 acquisition 段（_run_daily_retention_once）与 site stats 段
# （_run_site_stats_retention_once）各自独立函数/线程/开关，互不牵连
# --------------------------------------------------------------------------- #
def test_run_daily_retention_once_runs_acquisition_segment(monkeypatch):
    """acquisition 段：_run_daily_retention_once 只做来源触点清理
    （R2-F5 拆分后不再连带 site stats）。"""
    calls = []
    monkeypatch.setattr(app_mod.acquisition_store, "run_visit_retention",
                        lambda: calls.append("acq") or (3, 2))

    def _unexpected():
        raise AssertionError("R2-F5 拆分后 site stats 段不应被 "
                             "_run_daily_retention_once 调用")

    monkeypatch.setattr(sss, "purge_expired", _unexpected)
    monkeypatch.setattr(app_mod, "site_stats_store", sss)
    app_mod._run_daily_retention_once()
    assert calls == ["acq"]


def test_run_site_stats_retention_once_runs_purge(monkeypatch):
    """site stats 段：_run_site_stats_retention_once 只跑 purge_expired
    （store 非 None），不做 acquisition 清理。"""
    calls = []
    monkeypatch.setattr(sss, "purge_expired",
                        lambda: calls.append("purge") or 5)
    monkeypatch.setattr(app_mod, "site_stats_store", sss)

    def _unexpected():
        raise AssertionError("R2-F5 拆分后 acquisition 段不应被 "
                             "_run_site_stats_retention_once 调用")

    monkeypatch.setattr(app_mod.acquisition_store, "run_visit_retention",
                        _unexpected)
    app_mod._run_site_stats_retention_once()
    assert calls == ["purge"]


def test_retention_segments_do_not_take_each_other_down(monkeypatch):
    """互不拖垮：任一段异常不外泄、另一段照常执行（各段独立 try/except）。"""
    calls = []

    def _acq_boom():
        raise RuntimeError("acq down")

    monkeypatch.setattr(app_mod.acquisition_store, "run_visit_retention",
                        _acq_boom)
    monkeypatch.setattr(sss, "purge_expired",
                        lambda: calls.append("purge") or 4)
    monkeypatch.setattr(app_mod, "site_stats_store", sss)
    app_mod._run_daily_retention_once()          # acquisition 异常被吞
    app_mod._run_site_stats_retention_once()
    assert calls == ["purge"]
    # 反向：site stats 段异常，acquisition 段照常执行
    calls.clear()

    def _purge_boom():
        raise RuntimeError("stats down")

    monkeypatch.setattr(app_mod.acquisition_store, "run_visit_retention",
                        lambda: calls.append("acq") or (1, 0))
    monkeypatch.setattr(sss, "purge_expired", _purge_boom)
    app_mod._run_daily_retention_once()
    app_mod._run_site_stats_retention_once()    # purge 异常被吞
    assert calls == ["acq"]


def test_run_site_stats_retention_once_skips_missing_store(monkeypatch):
    """store 未随镜像发布（import 容错 None）：site stats 段静默跳过——
    与 _start_site_stats_worker 同口径。"""

    def _unexpected():
        raise AssertionError("store 为 None 时 purge_expired 不应被调用")

    monkeypatch.setattr(sss, "purge_expired", _unexpected)
    monkeypatch.setattr(app_mod, "site_stats_store", None)
    app_mod._run_site_stats_retention_once()    # 不抛即通过


def test_acq_interval_zero_does_not_affect_site_stats_segment(monkeypatch):
    """review R2-F5 核心契约：运维把 ACQ 间隔设 0（归因已退役的合理操作）
    只关掉 acquisition 调度线程，site stats 段开关/执行完全独立（R2-F5 拆分
    理由）。函数级验证：monkeypatch 后直接调 _run_site_stats_retention_once，
    不依赖 import 期线程。"""
    calls = []

    def _no_acq():
        raise AssertionError("site stats 段不应做 acquisition 清理")

    monkeypatch.setenv("ACQ_RETENTION_INTERVAL_SECONDS", "0")
    # ACQ 间隔=0 → acquisition 调度线程不启动（budget 特性不可用时本就 None）
    if app_mod.platform_features.budget_features_available():
        assert app_mod._start_acquisition_retention_thread() is None
    # 同一 env 前提下，site stats 单轮清理照常执行（独立函数，不触 acquisition）
    monkeypatch.setattr(app_mod.acquisition_store, "run_visit_retention",
                        _no_acq)
    monkeypatch.setattr(sss, "purge_expired",
                        lambda: calls.append("purge") or 7)
    monkeypatch.setattr(app_mod, "site_stats_store", sss)
    app_mod._run_site_stats_retention_once()
    assert calls == ["purge"]
    # site stats 自己的开关独立生效：间隔=0 关闭；正间隔以契约线程名启动
    monkeypatch.setenv("SITE_STATS_RETENTION_INTERVAL_SECONDS", "0")
    assert app_mod._start_site_stats_retention_thread() is None
    monkeypatch.setenv("SITE_STATS_RETENTION_INTERVAL_SECONDS", "86400")
    th = app_mod._start_site_stats_retention_thread()
    assert th is not None and th.name == "site-stats-retention" and th.daemon


@pytest.mark.parametrize("path", ["/wp-admin/install.php", "/.env", "/.git/config"])
def test_unknown_probe_does_not_redirect_to_login(monkeypatch, path):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    response = app_mod.app.test_client().get(path)
    assert response.status_code == 404
    assert "Location" not in response.headers


@pytest.mark.parametrize("ua", ["okhttp/4.12.0", "Mozilla/5.0 RobotPhone Chrome/130.0 Safari/537.36"])
def test_mobile_client_is_not_crawler_identity(ua):
    assert sss._classify_user_agent(ua) is None


def test_app_site_stats_warn_first_log_fires_on_fresh_boot(monkeypatch, caplog):
    """app._warn_site_stats_throttled：全新主机（monotonic < 300s）首条必发。

    节流状态以 None（从未记过）为哨兵——0.0 起始与 monotonic 比较会把
    刚开机主机（CI runner）的首条故障告警误判为窗口内而丢失。
    """
    app_mod._site_stats_warn_last["ts"] = None
    monkeypatch.setattr(time, "monotonic", lambda: 5.0)
    with caplog.at_level(logging.WARNING):
        app_mod._warn_site_stats_throttled("访问采集失败（fresh-boot 回归）")
    recs = [r for r in caplog.records if "[site-stats]" in r.getMessage()]
    assert len(recs) == 1
    app_mod._site_stats_warn_last["ts"] = None
