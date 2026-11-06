# -*- coding: utf-8 -*-
"""注册验证邮件 worker（I 线，设计文档第 8 节；表 registration_mail_jobs）。

命名红线：本模块与表名**禁止**叫 outbox（billing 语义已占用）。

职责边界（MailSender 适配边界）：
  - 本模块是「发送实现」的唯一适配层：registration_store / app.py 只入队
    （同事务 INSERT registration_mail_jobs），绝不直接触达任何邮件通道；
  - 发送实现经 :func:`get_sender` 按 env 装配：
      * ``REGISTRATION_MAIL_SENDER=fake`` → :class:`FakeMailSender`
        （测试假发送器：内存捕获，必须永不在生产配置）；
      * ``REGISTRATION_MAIL_SENDER=agent_mail_cli`` →
        :class:`AgentMailCliSender`（Agent Mail CLI 适配器：exec 风格
        **参数数组**、无 shell、两步 confirmation_token；凭据由 CLI 自身
        配置/环境持有，不进本仓库、不进日志）；
      * ``REGISTRATION_MAIL_SENDER=smtp`` → :class:`SmtpMailSender`
        （SSL/STARTTLS SMTP；账号口令只从环境变量读取，日志不回传）；
      * ``REGISTRATION_MAIL_SENDER=tencent_ses`` →
        :class:`TencentSesMailSender`（官方 SDK、审核模板、无隐式重试）；
      * 未配置 → ``None``：``email_verify_invite_activation`` 模式的前置
        检查（app 层）据此 fail-closed——sender 未配置时新模式**不能开启**。
  - 载荷安全：验证链接含**明文 token**，冻结正文在入队前经
    :func:`encrypt_payload`（Fernet）加密后才落库；库内只有
    ``token_hash``（registration_store 的域分离 HMAC）与密文载荷。明文
    token 只存在于「入队返回值 → 邮件正文」一条路径上。

worker 循环：``python registration_mail_worker.py --loop``（生产）；``--once``
单次排水（部署钩子/测试）。app 层入队后另有线程内 best-effort 即时排水
（失败不重试路径安全：作业保持 queued，worker 循环是权威发送方）。

P1-1/P1-2 语义（review）：
  - 发送阶段划分：DATA/确认步**之前**的失败=确定未发出（failed，有界指数
    退避重试）；请求发出后未收到远端最终响应=不确定（uncertain，绝不自动
    重发，防重复邮件/重复建号，留人工核对）；
  - 注册模式停机：排水前与 app 层共用 registration_store 的生效模式判定，
    非开放注册形态（email_verify_invite_activation / public）时
    purpose='email_verify' 作业保留 queued 暂停；purpose='registration_
    created'（P1 §4.5 已成功注册的管理员通知）与账户服务邮件在任何模式
    下照常排水；
  - 恢复开放后只发未过期作业（expires_at 已过期的保持 queued，验证端报
    expired）。
"""

import argparse
import base64
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import psycopg

import pg_store

_log = logging.getLogger("svs.registration_mail")

#: 验证 token 有效期（设计文档第 8 节：30 分钟）
VERIFY_TOKEN_TTL_SECONDS = 30 * 60

#: 单批排水上限（单条独立事务，SMTP 慢不长期持锁跨作业）
_DRAIN_BATCH = 20

#: CLI 单步超时（秒）
_CLI_TIMEOUT_SECONDS = 20


class MailSenderError(RuntimeError):
    """发送**确定未发出**（DATA/确认步之前失败或远端明确回绝）。

    worker 按 status=failed 记录并有界重试（指数退避 scheduled_at，attempts
    达上限后保持 failed 不再发送）。"""


class MailSenderUncertainError(MailSenderError):
    """发送**结果不确定**（P1-1）：请求已完整发出但未收到远端最终接受/拒绝
    响应——远端**可能已接受**，用户可能已收到邮件。

    worker 置 status=uncertain 且**绝不自动重发**（防重复邮件/重复建号），
    只留状态供人工核对；验证端在有效期内接受 uncertain 作业的 token。
    注意：本类是 MailSenderError 的子类，调用方 except 顺序必须**先捕获本类**。
    """


class MailSenderUnavailable(MailSenderError):
    """发送通道未配置/不可用（模式前置检查 fail-closed 依据）。"""


#: 确定失败的有界重试上限：attempts 达到后保持 status=failed 不再发送（P1-1）
_MAX_SEND_ATTEMPTS = 5
#: 指数退避基数（秒）：第 n 次失败（0 起数）后顺延 base * 2^n —— 30s/1m/2m/4m
_RETRY_BACKOFF_BASE_SECONDS = 30


# --------------------------------------------------------------------------- #
# 载荷加密（Fernet；cryptography 已在 requirements，与 AI api_key 加密同依赖）
# --------------------------------------------------------------------------- #
def _payload_secret() -> str:
    """载荷加密根 secret：REGISTRATION_MAIL_PAYLOAD_KEY → SECRET_KEY →
    数据目录 flask_secret.key（与 app secret 同源持久化，跨进程稳定）。"""
    for name in ("REGISTRATION_MAIL_PAYLOAD_KEY", "SECRET_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    data_dir = Path(
        os.environ.get("SHARE_DATA_DIR")
        or (Path.home() / "svs-viewer" / "share-data"))
    secret_file = data_dir / "flask_secret.key"
    try:
        v = secret_file.read_text(encoding="utf-8").strip()
    except OSError:
        v = ""
    if v:
        return v
    raise MailSenderUnavailable(
        "邮件载荷加密密钥不可用（REGISTRATION_MAIL_PAYLOAD_KEY / SECRET_KEY /"
        " flask_secret.key 均未配置）")


def payload_key_available() -> bool:
    """载荷加密密钥是否可用（email_verify_invite_activation 前置检查用）。"""
    try:
        _payload_secret()
        return True
    except Exception:
        return False


def _fernet():
    from cryptography.fernet import Fernet
    digest = hashlib.sha256(
        ("reg-mail-payload-v1:" + _payload_secret()).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_payload(payload: dict) -> str:
    """冻结正文（dict，含验证链接=含明文 token）→ Fernet 密文字符串。"""
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return _fernet().encrypt(blob.encode("utf-8")).decode("ascii")


def decrypt_payload(payload_enc: str) -> dict:
    """密文 → 冻结正文 dict；解密/解析失败抛 MailSenderError（记 failed）。"""
    try:
        blob = _fernet().decrypt(str(payload_enc).encode("ascii"))
        out = json.loads(blob.decode("utf-8"))
    except Exception as exc:
        raise MailSenderError(
            "payload_decrypt_failed（%s）" % exc.__class__.__name__) from exc
    if not isinstance(out, dict):
        raise MailSenderError("payload_shape_invalid")
    return out


# --------------------------------------------------------------------------- #
# 正文渲染（token 只进返回值；调用方加密后才落库）
# --------------------------------------------------------------------------- #
def public_base_url(environ=None):
    """公网入口 origin（去尾斜杠）。未配置返回 ""。

    与 registration_store 的前置检查同口径读 ``PUBLIC_BASE_URL``；只负责
    取出规范形式，**不**做 https 强制（强校验在 registration_store 前置闸与
    app 层 CSP 装配）。供需要构造管理/登录链接的调用方（如 feedback_store）
    复用，避免各处重复 env 读取。
    """
    env = os.environ if environ is None else environ
    return (env.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")


def build_verify_email_body(email, token, base_url):
    """构造验证邮件冻结正文。返回 (subject, body)。

    链接 = ``<base_url>/verify-email?token=<明文 token>``；GET /verify-email
    只展示不消费（消费在 POST /api/registration/verify）。

    R2（2026-09-19）文案同步：与注册弹窗同一口径——「验证邮箱并提交申请，
    管理员审核通过后即可使用」；删除「验证邮箱本身不会授予任何工作区、AI 或
    额度权限」等实现型说明；保留邀请码直接激活的替代路径提示。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise MailSenderUnavailable("PUBLIC_BASE_URL 未配置，无法构造验证链接")
    link = base + "/verify-email?token=" + str(token)
    subject = "PathTogether 邮箱验证（30 分钟内有效）"
    body = (
        "你好，\n\n"
        "有人（通常是你本人）刚用邮箱 %s 请求注册 PathTogether。\n"
        "请在 30 分钟内打开下面的链接完成邮箱验证：\n\n"
        "%s\n\n"
        "该链接只能使用一次。验证后请设置密码并提交使用申请，管理员审核通过"
        "后即可使用；如果你已有邀请码，也可以凭邀请码直接激活。\n\n"
        "如果你没有请求过注册，请忽略本邮件。\n"
        % (str(email), link))
    return subject, body


def build_public_verify_email_body(email, token, base_url):
    """public 自助注册的验证邮件冻结正文（P1，docs §3.3/§4.1）。返回
    (subject, body)。

    文案口径与协议文稿/注册弹窗一致：每日最多 5 个新自助账号、名额于北京
    时间每日 00:00 更新、以完成注册时的剩余名额为准——**不承诺发送邮件
    即已预留名额**；不提前收密码、不提前建可用账号（§3.3.1）。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise MailSenderUnavailable("PUBLIC_BASE_URL 未配置，无法构造验证链接")
    link = base + "/verify-email?token=" + str(token)
    subject = "PathTogether 邮箱验证（30 分钟内有效）"
    body = (
        "你好，\n\n"
        "有人（通常是你本人）刚用邮箱 %s 请求注册 PathTogether。\n"
        "请在 30 分钟内打开下面的链接完成注册：\n\n"
        "%s\n\n"
        "该链接只能使用一次。验证后设置密码即可直接使用，无需管理员审批。\n"
        "当前开放邮箱验证注册，每日最多 5 个新自助账号，名额于北京时间每日 "
        "00:00 更新，以完成注册时的剩余名额为准；发送或收到本邮件不代表已"
        "预留名额。若链接过期，请重新申请验证邮件。\n\n"
        "如果你没有请求过注册，请忽略本邮件。\n"
        % (str(email), link))
    return subject, body


def build_verify_email_body_for_site(email, token, *, entry_origin,
                                     form_locale="zh", flow="legacy"):
    """按**冻结入口与语言**构造验证邮件正文（registration-antibot 设计
    2026-10-08 §6/§9）：站点名、验证链接、帮助页链接全部来自
    ``entry_origin``（入队时冻结；本函数绝不读 PUBLIC_BASE_URL/env 选
    域名），语言取 ``form_locale``（zh|en）；``flow`` ∈ public|legacy
    决定文案口径。

    供 registration_store.request_verification_email 在入队/重发时调用
    （构造发生在 app 侧事务内；worker 只发送冻结密文）。帮助链接为该
    入口域名上的绝对 /registration-help 链接。
    """
    import registration_antibot
    origin = str(entry_origin or "").strip().rstrip("/")
    if not origin:
        raise MailSenderUnavailable(
            "注册入口 origin 未配置，无法构造验证链接")
    site = registration_antibot.site_name_for_origin(origin)
    link = origin + "/verify-email?token=" + str(token)
    help_link = origin + "/registration-help"
    if str(form_locale or "zh").strip().lower() == "en":
        subject = "%s email verification (valid for 30 minutes)" % site
        if flow == "public":
            body = (
                "Hello,\n\n"
                "Someone (usually you) just requested a %s account for "
                "%s.\n"
                "Open the link below within 30 minutes to complete "
                "registration:\n\n%s\n\n"
                "The link can only be used once. After verifying, set a "
                "password and the account is ready — no admin approval "
                "needed. Self-service registration allows up to 5 new "
                "accounts per day (refreshed at 00:00 Beijing time, "
                "subject to remaining quota at completion); receiving "
                "this email does not reserve a slot.\n\n"
                "If the email never arrives or you cannot finish "
                "registration, visit %s to contact the author.\n\n"
                "If you did not request this, please ignore this email.\n"
                % (site, str(email), link, help_link))
        else:
            body = (
                "Hello,\n\n"
                "Someone (usually you) just requested %s registration "
                "for %s.\n"
                "Open the link below within 30 minutes to verify this "
                "email address:\n\n%s\n\n"
                "The link can only be used once. After verifying, set a "
                "password and submit your application; if you already "
                "have an invitation code, you can activate directly.\n\n"
                "If the email never arrives or verification fails, visit "
                "%s to contact the author.\n\n"
                "If you did not request this, please ignore this email.\n"
                % (site, str(email), link, help_link))
        return subject, body
    subject = "%s 邮箱验证（30 分钟内有效）" % site
    if flow == "public":
        body = (
            "你好，\n\n"
            "有人（通常是你本人）刚用邮箱 %s 请求注册 %s。\n"
            "请在 30 分钟内打开下面的链接完成注册：\n\n"
            "%s\n\n"
            "该链接只能使用一次。验证后设置密码即可直接使用，无需管理员"
            "审批。\n当前开放邮箱验证注册，每日最多 5 个新自助账号，名额于"
            "北京时间每日 00:00 更新，以完成注册时的剩余名额为准；发送或"
            "收到本邮件不代表已预留名额。若链接过期，请重新申请验证邮件。\n"
            "如长时间未收到邮件或无法完成注册，可访问 %s 联系作者。\n\n"
            "如果你没有请求过注册，请忽略本邮件。\n"
            % (str(email), site, link, help_link))
    else:
        body = (
            "你好，\n\n"
            "有人（通常是你本人）刚用邮箱 %s 请求注册 %s。\n"
            "请在 30 分钟内打开下面的链接完成邮箱验证：\n\n"
            "%s\n\n"
            "该链接只能使用一次。验证后请设置密码并提交使用申请，管理员"
            "审核通过后即可使用；如果你已有邀请码，也可以凭邀请码直接激"
            "活。\n如长时间未收到邮件或无法完成验证，可访问 %s 联系作者。\n\n"
            "如果你没有请求过注册，请忽略本邮件。\n"
            % (str(email), site, link, help_link))
    return subject, body


def build_registration_created_body(*, user_id, email, source, day,
                                    successful_count, daily_limit, base_url):
    """自助注册成功的管理员通知正文（P1，docs §4.5）。返回 (subject, body)。

    正文**仅**包含：注册时间、账号 ID/邮箱、来源模式、当天成功数、管理员
    用户列表链接。**默认不含研究共享选择**（避免管理员以此区别对待用
    户）；不含密码、验证链接、会话 cookie、对话、图像、行为轨迹或可直接
    修改账号的令牌。
    """
    base = str(base_url or "").strip().rstrip("/")
    admin_link = (base + "/admin/users") if base else "（PUBLIC_BASE_URL 未配置）"
    subject = "PathTogether · 新自助注册（%s 当日第 %d/%d 个）" % (
        str(day), int(successful_count), int(daily_limit))
    body = (
        "新自助注册账号\n\n"
        "注册时间：%s（Asia/Shanghai 日桶）\n"
        "账号 ID：%s\n"
        "邮箱：%s\n"
        "来源模式：%s\n"
        "当天成功数：%d/%d\n"
        "管理台用户列表：%s\n"
        % (str(day), str(user_id), str(email), str(source),
           int(successful_count), int(daily_limit), admin_link))
    return subject, body


# --------------------------------------------------------------------------- #
# MailSender 适配边界
# --------------------------------------------------------------------------- #
class FakeMailSender:
    """测试假发送器（必须提供的首个实现）：内存捕获，永不真实外发。

    仅限测试装配（``REGISTRATION_MAIL_SENDER=fake`` 或直接 monkeypatch
    :func:`get_sender`）；生产前置检查把 ``fake`` 视为**未配置生产通道**
    （sender_configured(production=True)=False），防止测试通道漏进生产。
    """

    def __init__(self):
        self.sent = []  # [(to, subject, body)]

    def send(self, to, subject, body):
        self.sent.append((str(to), str(subject), str(body)))

    def clear(self):
        self.sent.clear()


#: 模块级单例（测试经 install_fake_sender 取用）
_fake_sender = FakeMailSender()


def install_fake_sender():
    """返回模块级 FakeMailSender 单例（测试注入入口）。"""
    global _fake_sender
    return _fake_sender


class AgentMailCliSender:
    """Agent Mail CLI 适配器（两步 confirmation_token）。

    - ``subprocess.run`` **参数数组**（无 shell、无字符串拼接命令行）；
    - 两步：``send-request``（stdin 传正文）→ 解析 JSON 输出里的
      ``confirmation_token`` → ``send-confirm`` 提交确认；第二步确认后才
      真正外发（幂等防护由 CLI 侧负责）；
    - 凭据：由 CLI 自身配置/环境持有；本类**不**接收、不拼接、不记录任何
      凭据，日志与异常文本只含类别（argv/env 绝不进日志）。
    """

    def __init__(self, cli_path, sender=None):
        self.cli_path = str(cli_path or "").strip()
        self.sender = str(sender or "").strip() or None
        if not self.cli_path:
            raise MailSenderUnavailable("REGISTRATION_AGENT_MAIL_CLI 未配置")

    def _run(self, argv, stdin_text=None, confirm_phase=False):
        """执行一步 CLI（参数数组，无 shell）。

        ``confirm_phase=True`` 标注第二步 send-confirm：该步请求已提交、若未
        收到 CLI 响应（超时/执行中断）则**远端可能已确认发送**——按 P1-1 划
        分为不确定（MailSenderUncertainError），绝不当 failed 自动重试。
        """
        try:
            proc = subprocess.run(
                [self.cli_path] + list(argv),  # 参数数组；绝无 shell
                input=stdin_text, capture_output=True, text=True,
                timeout=_CLI_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            # CLI 缺失=确认步根本未发出（确定未发送），两阶段同语义
            raise MailSenderUnavailable(
                "agent_mail_cli_not_found") from exc
        except subprocess.TimeoutExpired as exc:
            if confirm_phase:
                raise MailSenderUncertainError(
                    "agent_mail_cli_confirm_timeout") from exc
            raise MailSenderError("agent_mail_cli_timeout") from exc
        except OSError as exc:
            if confirm_phase:
                raise MailSenderUncertainError(
                    "agent_mail_cli_confirm_no_response（%s）"
                    % exc.__class__.__name__) from exc
            raise MailSenderError(
                "agent_mail_cli_exec_failed（%s）" % exc.__class__.__name__
            ) from exc
        if proc.returncode != 0:
            # CLI 已给出明确回绝（exit code=收到了响应）→ 确定未发出；
            # 不回传 stderr 原文（可能含本机路径/凭据线索），只记类别
            raise MailSenderError(
                "agent_mail_cli_exit_%d" % int(proc.returncode))
        return proc.stdout

    def send(self, to, subject, body):
        # 阶段 1（确定未发出区间）：send-request 未成功、确认令牌未拿到——
        # 远端两步协议下必未真正外发，一切失败按 MailSenderError（可重试）
        req_argv = ["send-request", "--to", str(to), "--subject",
                    str(subject)]
        if self.sender:
            req_argv += ["--from", self.sender]
        out = self._run(req_argv, stdin_text=str(body))
        try:
            payload = json.loads(out)
            token = str(payload["confirmation_token"])
        except Exception as exc:
            raise MailSenderError(
                "agent_mail_cli_bad_response（%s）"
                % exc.__class__.__name__) from exc
        if not token:
            raise MailSenderError("agent_mail_cli_empty_confirmation_token")
        # 阶段 2（不确定窗口）：send-confirm 已提交、响应未收到=远端可能已
        # 确认外发（P1-1：置 uncertain 不自动重发）；exit code 非零=CLI 明确
        # 回绝（确定未发出，仍按 failed）
        self._run(["send-confirm", "--confirmation-token", token],
                  confirm_phase=True)


class SmtpMailSender:
    """SSL/STARTTLS SMTP 发送器（163 等 IMAP/SMTP 授权码通道）。

    环境变量（均 trim；口令绝不进日志/异常原文）：
      REGISTRATION_SMTP_HOST / REGISTRATION_SMTP_PORT（默认 465）
      REGISTRATION_SMTP_USER / REGISTRATION_SMTP_PASSWORD
      REGISTRATION_SMTP_FROM（缺省=USER）
      REGISTRATION_SMTP_STARTTLS=1 时走 587 STARTTLS，否则 SMTP_SSL。

      P1-1 不确定态的阶段划分（见 :meth:`_transmit`）：DATA 结束符开始写出
      **之前**的任何失败=确定未发出（failed，可重试）；从开始发送结束符起
      （含结束符 send 本身与等待最终响应）的超时/断连=结果不确定
      （uncertain，绝不自动重发）。
    """

    def __init__(self, environ=None):
        env = os.environ if environ is None else environ
        self.host = (env.get("REGISTRATION_SMTP_HOST") or "").strip()
        self.user = (env.get("REGISTRATION_SMTP_USER") or "").strip()
        self.password = env.get("REGISTRATION_SMTP_PASSWORD") or ""
        self.from_addr = ((env.get("REGISTRATION_SMTP_FROM") or "").strip()
                          or self.user)
        try:
            self.port = int((env.get("REGISTRATION_SMTP_PORT") or "465").strip()
                            or "465")
        except ValueError as exc:
            raise MailSenderUnavailable("registration_smtp_port_invalid") from exc
        self.starttls = (env.get("REGISTRATION_SMTP_STARTTLS") or "").strip().lower() in (
            "1", "true", "yes", "on")
        if not self.host or not self.user or not self.password:
            raise MailSenderUnavailable("registration_smtp_not_configured")

    def _transmit(self, smtp, msg_str, to_addr, ctx):
        """分阶段 SMTP 事务（P1-1 划分边界）。

        阶段 1（确定未发出区间）：EHLO / STARTTLS / AUTH / MAIL FROM /
        RCPT TO / DATA(354) 与正文写出——此段任何失败（含正文 socket 写失败：
        sendall 语义保证数据未完整送达、远端事务中止）都意味着邮件**确定未
        发出**，抛 MailSenderError（worker 记 failed 可重试）；
        阶段 2（不确定窗口）：正文与结束符 ``.`` 已全部写出、等待远端对整个
        事务的最终响应——此段超时/断连意味着远端**可能已接受**，抛
        MailSenderUncertainError（worker 置 uncertain，绝不自动重发）。
        """
        import smtplib

        # ---- 阶段 1 ----
        code, _resp = smtp.ehlo()
        if code != 250:
            raise MailSenderError("smtp_ehlo_rejected_%d" % int(code))
        if self.starttls:
            code, _resp = smtp.starttls(context=ctx)
            if code != 220:
                raise MailSenderError(
                    "smtp_starttls_rejected_%d" % int(code))
            code, _resp = smtp.ehlo()
            if code != 250:
                raise MailSenderError("smtp_ehlo_rejected_%d" % int(code))
        # 认证失败 → SMTPAuthenticationError（send 层统一译为 smtp_auth_failed）
        smtp.login(self.user, self.password)
        code, _resp = smtp.docmd("MAIL", "FROM:<%s>" % self.from_addr)
        if code != 250:
            raise MailSenderError("smtp_mail_from_rejected_%d" % int(code))
        code, _resp = smtp.docmd("RCPT", "TO:<%s>" % to_addr)
        if code not in (250, 251):
            raise MailSenderError("smtp_rcpt_rejected_%d" % int(code))
        code, _resp = smtp.docmd("DATA")
        if code != 354:
            raise MailSenderError("smtp_data_start_rejected_%d" % int(code))
        # 正文写出（与 smtplib.SMTP.data 对 str 的处理同款：CRLF 归一 + 行首
        # 点引用/转义；本处本地实现等价逻辑，避免依赖 smtplib 私有助手）
        import re as _re
        data = _re.sub(br"(?:\r\n|\n|\r(?!\n))", b"\r\n",
                       msg_str.encode("ascii"))
        data = _re.sub(br"(?m)^\.", b"..", data)
        smtp.send(data)
        # ---- 阶段 2（不确定窗口）：从开始发送 DATA 结束符起 ----
        # 结束符一旦开始写出，客户端即无法证明远端未完整接收并提交整个
        # DATA 事务（TCP send 缓冲/对端已读都不可观测）。因此结束符 send
        # 本身的超时/断连与 getreply 超时同一语义：结果不确定，绝不重发。
        # （二轮 review P1-1：此前结束符 send 在阶段 1，TimeoutError 会被
        # 外层 except OSError 译成可重试 MailSenderError，可能重复发信。）
        try:
            smtp.send(b".\r\n")
            code, _resp = smtp.getreply()
        except Exception as exc:
            raise MailSenderUncertainError(
                "smtp_final_response_missing（%s）"
                % exc.__class__.__name__) from exc
        if code != 250:
            # 远端明确回绝（未接受）→ 仍属确定未发出
            raise MailSenderError("smtp_data_rejected_%d" % int(code))

    def send(self, to, subject, body):
        import smtplib
        import ssl
        from email.header import Header
        from email.mime.text import MIMEText
        from email.utils import formataddr, formatdate, make_msgid

        to_addr = str(to).strip()
        if not to_addr or "\n" in to_addr or "\r" in to_addr:
            raise MailSenderError("smtp_recipient_invalid")
        msg = MIMEText(str(body), "plain", "utf-8")
        msg["Subject"] = Header(str(subject), "utf-8")
        msg["From"] = formataddr(("PathTogether", self.from_addr))
        msg["To"] = to_addr
        msg["Date"] = formatdate(localtime=True)
        domain = self.from_addr.rsplit("@", 1)[-1] if "@" in self.from_addr else "localhost"
        msg["Message-ID"] = make_msgid(domain=domain)
        ctx = ssl.create_default_context()
        try:
            if self.starttls:
                smtp = smtplib.SMTP(self.host, self.port, timeout=30)
            else:
                smtp = smtplib.SMTP_SSL(self.host, self.port, context=ctx,
                                        timeout=30)
        except OSError as exc:
            raise MailSenderError(
                "smtp_connect_failed（%s）" % exc.__class__.__name__) from exc
        try:
            with smtp:
                self._transmit(smtp, msg.as_string(), to_addr, ctx)
        except MailSenderError:
            raise  # 含 MailSenderUncertainError（子类）：阶段语义由 _transmit 决定，原样透传
        except smtplib.SMTPAuthenticationError as exc:
            raise MailSenderError("smtp_auth_failed") from exc
        except smtplib.SMTPException as exc:
            raise MailSenderError(
                "smtp_send_failed（%s）" % exc.__class__.__name__) from exc
        except OSError as exc:
            # DATA 之前的连接级错误（确定未发出；正文写失败已在 sendall 语义
            # 下归入本类）
            raise MailSenderError(
                "smtp_send_failed（%s）" % exc.__class__.__name__) from exc


class TencentSesMailSender:
    """腾讯云 SES 模板发送；模板参数在入队时与正文一起加密冻结。

    SDK 不重试、不切地域；调用后的未知异常一律 uncertain，防止重复邮件。
    只输出固定错误类别，不传播 SDK message（可能含凭据或验证链接）。
    """

    TEMPLATE_KEYS = frozenset(("email_verify_zh", "email_verify_en",
                               "email_change_zh", "registration_created_zh",
                               "user_feedback_zh", "email_verify_zh_com",
                               "email_verify_en_cn", "email_change_zh_com",
                               "registration_created_zh_com"))

    def __init__(self, environ=None):
        from email.utils import parseaddr
        import registration_store

        env = os.environ if environ is None else environ
        self.region = (env.get("REGISTRATION_SES_REGION") or "ap-guangzhou").strip()
        self.from_addr = (env.get("REGISTRATION_SES_FROM") or "").strip()
        self.reply_to = (env.get("REGISTRATION_SES_REPLY_TO") or "").strip()
        sid = (env.get("REGISTRATION_SES_SECRET_ID") or "").strip()
        skey = (env.get("REGISTRATION_SES_SECRET_KEY") or "").strip()
        token = (env.get("REGISTRATION_SES_TOKEN") or "").strip() or None
        try:
            ids = json.loads(env.get("REGISTRATION_SES_TEMPLATE_IDS") or "{}")
            if not isinstance(ids, dict) or set(ids) != self.TEMPLATE_KEYS:
                raise ValueError
            if any(type(v) is not int or v <= 0 for v in ids.values()):
                raise ValueError
            self.template_ids = ids
            if self.region not in ("ap-guangzhou", "ap-hongkong"):
                raise ValueError
            if not sid or not skey:
                raise ValueError
            # 腾讯支持「别名 <地址>」，别名不能含冒号；拒绝换行与多地址。
            _alias, address = parseaddr(self.from_addr)
            registration_store.validate_email(address)
            if any(c in self.from_addr for c in "\r\n,;:"):
                raise ValueError
            if self.reply_to:
                registration_store.validate_email(self.reply_to)
        except (ValueError, TypeError, registration_store.EmailVerifyError):
            raise MailSenderUnavailable("ses_configuration_invalid") from None
        try:
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.common.retry import NoopRetryer
            from tencentcloud.ses.v20201002 import ses_client, models

            # SDK 的 DEBUG 会输出签名请求头和 TemplateData（含明文 token）。
            logging.getLogger("tencentcloud_sdk_common").setLevel(logging.WARNING)
            profile = ClientProfile(
                httpProfile=HttpProfile(endpoint="ses.tencentcloudapi.com",
                                        reqTimeout=30),
                retryer=NoopRetryer(), disable_region_breaker=True)
            self.client = ses_client.SesClient(
                credential.Credential(sid, skey, token), self.region, profile)
            self.models = models
        except ImportError:
            raise MailSenderUnavailable("ses_sdk_unavailable") from None

    @staticmethod
    def _approved_template(key, data):
        """固定域名模板；从冻结链接提取令牌，绝不把完整 URL 作为变量。"""
        import re
        from urllib.parse import parse_qs, urlsplit

        if key == "user_feedback_zh":
            return key, {"feedback_id": data.get("feedback_id", "")}
        paths = {"email_verify_zh": "/verify-email",
                 "email_verify_en": "/verify-email",
                 "email_change_zh": "/verify-email-change",
                 "registration_created_zh": "/admin/users"}
        try:
            url = urlsplit(data["link"])
            if url.scheme != "https" or url.netloc not in (
                    "histopilot.cn", "histopilot.com") \
                    or url.fragment or url.path != paths[key]:
                raise ValueError
            if key == "registration_created_zh":
                if url.query or data.get("source") != "public_registration":
                    raise ValueError
                variables = {k: data[k] for k in (
                    "day", "user_id", "email", "count", "limit")}
            else:
                query = parse_qs(url.query, strict_parsing=True, keep_blank_values=True)
                if set(query) != {"token"} or len(query["token"]) != 1:
                    raise ValueError
                token = query["token"][0]
                if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
                    raise ValueError
                variables = {"email": data["email"], "token": token}
            if key == "email_verify_en":
                key += "_cn" if url.netloc == "histopilot.cn" else ""
            elif url.netloc == "histopilot.com":
                key += "_com"
            return key, variables
        except (KeyError, ValueError):
            raise MailSenderError("ses_template_link_invalid") from None

    def send_payload(self, to, payload):
        import registration_store
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
            TencentCloudSDKException,
        )

        try:
            recipient = registration_store.validate_email(to)
        except registration_store.EmailVerifyError:
            raise MailSenderError("ses_recipient_invalid") from None
        key = payload.get("template_key")
        data = payload.get("template_data")
        if not isinstance(key, str) or key not in self.template_ids \
                or not isinstance(data, dict) \
                or not all(isinstance(k, str) and isinstance(v, str)
                           for k, v in data.items()):
            # 历史队列必须在切换前用 SMTP 排空，不能猜正文或跨用途发模板。
            raise MailSenderError("ses_template_metadata_missing")
        key, data = self._approved_template(key, data)
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 800:
            raise MailSenderError("ses_template_data_too_large")
        params = {
            "FromEmailAddress": self.from_addr,
            "Destination": [recipient],
            "Subject": str(payload.get("subject") or ""),
            "Template": {"TemplateID": self.template_ids[key],
                         "TemplateData": encoded},
            "TriggerType": 1,
        }
        if self.reply_to:
            params["ReplyToAddresses"] = self.reply_to
        if key == "user_feedback_zh":
            # 反馈含诊断记录，不能塞进 800-byte TemplateData；保留完整正文。
            body = str(payload.get("body") or "").encode("utf-8")
            if len(body) > 4_000_000:
                raise MailSenderError("ses_attachment_too_large")
            params["Attachments"] = [{
                "FileName": "feedback.txt",
                "Content": base64.b64encode(body).decode("ascii"),
            }]
        request = self.models.SendEmailRequest()
        request.from_json_string(json.dumps(params, ensure_ascii=False))
        try:
            response = self.client.SendEmail(request)
        except TencentCloudSDKException as exc:
            # 只有携带 RequestId 的明确拒绝才可自动重试。内部错误也保守
            # 视为不确定；不把 SDK 自由文本、RequestId 或请求内容写入异常。
            code = exc.get_code() or ""
            rejected = ("AuthFailure", "UnauthorizedOperation", "InvalidParameter",
                        "MissingParameter", "UnsupportedOperation", "LimitExceeded",
                        "RequestLimitExceeded", "FailedOperation", "ResourceNotFound",
                        "ResourceUnavailable")
            if exc.get_request_id() and code.split(".")[0] in rejected:
                raise MailSenderError(
                    "ses_request_rejected_" + code.split(".")[0]) from None
            raise MailSenderUncertainError("ses_response_uncertain") from None
        except Exception:
            raise MailSenderUncertainError("ses_response_uncertain") from None
        if not isinstance(getattr(response, "MessageId", None), str) \
                or not response.MessageId.strip():
            raise MailSenderUncertainError("ses_message_id_missing")


def _send_payload(sender, to, payload):
    if isinstance(sender, TencentSesMailSender):
        sender.send_payload(to, payload)
    else:
        sender.send(to, str(payload.get("subject") or ""),
                    str(payload.get("body") or ""))


def verify_template_metadata(email, token, origin, locale):
    """与冻结验证正文同源；不从发送时 env 重建链接或语言。"""
    import registration_antibot
    origin = str(origin).rstrip("/")
    language = "en" if str(locale).strip().lower() == "en" else "zh"
    return {"template_key": "email_verify_" + language,
            "template_data": {
                "site": registration_antibot.site_name_for_origin(origin),
                "email": str(email),
                "link": origin + "/verify-email?token=" + str(token),
                "help_link": origin + "/registration-help",
            }}


def sender_configured(environ=None, production=True) -> bool:
    """邮件发送通道是否已配置（email_verify_invite_activation 前置检查）。

    ``production=True``（默认）：``fake`` 不算已配置（测试通道不放大到生产
    前置）；测试可显式传 ``production=False`` 或 monkeypatch get_sender。
    """
    env = os.environ if environ is None else environ
    kind = (env.get("REGISTRATION_MAIL_SENDER") or "").strip()
    if not kind:
        return False
    if kind == "fake":
        return not production
    if kind == "agent_mail_cli":
        return bool((env.get("REGISTRATION_AGENT_MAIL_CLI") or "").strip())
    if kind == "smtp":
        return bool((env.get("REGISTRATION_SMTP_HOST") or "").strip()
                    and (env.get("REGISTRATION_SMTP_USER") or "").strip()
                    and (env.get("REGISTRATION_SMTP_PASSWORD") or "").strip())
    if kind == "tencent_ses":
        return get_sender(env) is not None
    return False


def get_sender(environ=None):
    """按 env 装配发送器；未配置返回 None（入队照常，发送 fail 记录）。"""
    env = os.environ if environ is None else environ
    kind = (env.get("REGISTRATION_MAIL_SENDER") or "").strip()
    if kind == "fake":
        return _fake_sender
    if kind == "agent_mail_cli":
        return AgentMailCliSender(
            env.get("REGISTRATION_AGENT_MAIL_CLI"),
            env.get("REGISTRATION_AGENT_MAIL_FROM"))
    if kind == "smtp":
        try:
            return SmtpMailSender(env)
        except MailSenderUnavailable:
            return None
    if kind == "tencent_ses":
        try:
            return TencentSesMailSender(env)
        except MailSenderUnavailable:
            return None
    return None


# --------------------------------------------------------------------------- #
# 排水（权威发送循环）
# --------------------------------------------------------------------------- #
def _connect():
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def drain_once(limit=_DRAIN_BATCH, sender=None, environ=None) -> int:
    """处理至多 limit 条待发作业；返回成功发送条数。

    P1-2 模式停机语义：排水前查**生效注册模式**——与 app 层共用
    registration_store.resolve_effective_registration_mode 的权威判定（worker
    绝不 import Flask app）。非 email_verify_invite_activation → **注册类
    邮件（purpose='email_verify'）暂停**（保留 queued：不 fail、不计时、
    不改状态）；账户服务邮件（purpose='email_change'，登录用户的本人
    操作）不在注册停机范围内，照常发送（注册关闭不应波及已注册用户
    的本人邮件——P1-3 改绑闭环依赖此通道）。

    每条作业独立事务：``SELECT ... FOR UPDATE SKIP LOCKED``（行锁只在单条
    发送期间持有）→ 解密载荷 → sender.send → 按阶段分类落状态（P1-1）：

    - 成功 → status=sent；
    - :class:`MailSenderUncertainError`（远端可能已接受）→ status=uncertain，
      **不自动重发**（防重复邮件/重复建号），只留状态供人工核对；
    - :class:`MailSenderError`（确定未发出）→ status=failed，attempts+1，
      scheduled_at 按指数退避顺延（上限 :data:`_MAX_SEND_ATTEMPTS`，达上限
      保持 failed 不再发送）。

    领取范围只含 ``scheduled_at <= now()`` 且 ``expires_at > now()`` 的
    queued/未达上限 failed 行——恢复开放后只发未过期作业；已过期的保持
    queued（不发送、不 fail），由验证端报 expired。
    发送通道未配置 → 全部保留 queued（worker 前置未满足是部署问题，不是
    作业失败）。
    """
    import registration_store
    mode, _failures = registration_store.resolve_effective_registration_mode(
        environ)
    # 注册停只停注册类邮件（email_verify）；registration_created（已成功
    # 注册的管理员通知，P1 §4.5：注册暂停后仍发送）与 email_change 等账户
    # 服务邮件照常领取——「非旧模式就不发任何邮件」的判断被显式禁止。
    # registration_open=True 时不过滤 purpose。
    registration_open = (
        mode in (registration_store.MODE_EMAIL_VERIFY_INVITE_ACTIVATION,
                 registration_store.MODE_PUBLIC))
    if not registration_open:
        _log.info("生效注册模式为 %s（非开放注册形态）：email_verify 作业保"
                  "留 queued 暂停；registration_created/账户服务邮件照常",
                  mode)
    snd = sender if sender is not None else get_sender()
    if snd is None:
        _log.warning("邮件发送通道未配置（REGISTRATION_MAIL_SENDER），%d 条"
                     "作业保留 queued", limit)
        return 0
    sent = 0
    conn = _connect()
    try:
        for _ in range(max(1, min(int(limit), 200))):
            with pg_store.transaction(conn) as c:
                with c.cursor() as cur:
                    cur.execute(
                        "SELECT job_id, email_normalized, payload_enc, "
                        "payload_enc IS NULL AS bad_payload, attempts "
                        "FROM registration_mail_jobs "
                        "WHERE (status='queued' OR (status='failed' "
                        "AND attempts < %s)) "
                        "AND scheduled_at <= now() AND expires_at > now() "
                        "AND (%s OR purpose <> 'email_verify') "
                        "ORDER BY created_at, job_id LIMIT 1 FOR UPDATE "
                        "SKIP LOCKED",
                        (_MAX_SEND_ATTEMPTS, registration_open))
                    row = cur.fetchone()
                    if row is None:
                        break
                    try:
                        payload = decrypt_payload(row["payload_enc"])
                        _send_payload(snd, row["email_normalized"], payload)
                    except MailSenderUncertainError as exc:
                        # 先于 MailSenderError 捕获（子类）：远端可能已接受，
                        # 置 uncertain 且绝不自动重发（uncertain 不在领取
                        # 范围内），只留状态供人工核对
                        cur.execute(
                            "UPDATE registration_mail_jobs SET "
                            "status='uncertain', attempts=attempts+1, "
                            "last_error=%s WHERE job_id=%s",
                            (str(exc)[:120], row["job_id"]))
                        _log.warning("验证邮件发送结果不确定（job=%s 类别见 "
                                     "last_error）：置 uncertain，不自动"
                                     "重发", row["job_id"])
                        continue
                    except MailSenderError as exc:
                        attempts = int(row["attempts"] or 0)
                        if attempts + 1 >= _MAX_SEND_ATTEMPTS:
                            # 达重试上限：保持 failed，不再顺延 scheduled_at
                            # （领取范围含 attempts<上限，自然停发）
                            cur.execute(
                                "UPDATE registration_mail_jobs SET "
                                "status='failed', attempts=attempts+1, "
                                "last_error=%s WHERE job_id=%s",
                                (str(exc)[:120], row["job_id"]))
                        else:
                            # 确定未发出（DATA/确认步之前或远端明确回绝）：
                            # 指数退避后有界重试
                            delay = _RETRY_BACKOFF_BASE_SECONDS \
                                * (2 ** attempts)
                            cur.execute(
                                "UPDATE registration_mail_jobs SET "
                                "status='failed', attempts=attempts+1, "
                                "last_error=%s, scheduled_at=now() + "
                                "(%s * interval '1 second') "
                                "WHERE job_id=%s",
                                (str(exc)[:120], delay, row["job_id"]))
                        _log.warning("验证邮件发送失败（job=%s 第 %d/%d 次"
                                     "尝试，类别见 last_error）",
                                     row["job_id"], attempts + 1,
                                     _MAX_SEND_ATTEMPTS)
                        continue
                    cur.execute(
                        "UPDATE registration_mail_jobs SET status='sent', "
                        "attempts=attempts+1, sent_at=now(), last_error=NULL "
                        "WHERE job_id=%s", (row["job_id"],))
            sent += 1
        # 0079：重发投递同样由 worker 排水（独立循环，原作业排空后继续）
        sent += _drain_redeliveries(conn, snd, limit, registration_open)
    finally:
        conn.close()
    return sent


def _drain_redeliveries(conn, snd, limit, registration_open) -> int:
    """0079 重发投递排水（drain_once 内调用；每条独立事务）。

    领取 queued/未达上限 failed 的 registration_mail_redeliveries；发送前
    锁原作业行并复核未消费/未作废/未过期且 intent 未完成（§4：注册完成、
    token 作废、链接过期、模式关闭都能阻止尚未发出的重发）；复核不通过
    置 cancelled（不再领取）。失败分类与原作业同规则：确定失败有界重试、
    uncertain 不自动重试。返回成功发送条数。
    """
    if not registration_open:
        return 0
    sent = 0
    for _ in range(max(1, min(int(limit), 200))):
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT r.redelivery_id, r.job_id, r.email_normalized, "
                    "r.payload_enc, r.attempts "
                    "FROM registration_mail_redeliveries r "
                    "WHERE (r.status='queued' OR (r.status='failed' "
                    "  AND r.attempts < %s)) "
                    "AND r.scheduled_at <= now() "
                    "ORDER BY r.created_at, r.redelivery_id "
                    "LIMIT 1 FOR UPDATE OF r SKIP LOCKED",
                    (_MAX_SEND_ATTEMPTS,))
                rrow = cur.fetchone()
                if rrow is None:
                    break
                # 发送前锁原作业行并复核仍可发（§4；与完成事务互斥串行）；
                # 复核不通过 → cancelled，不发送、不再领取
                cur.execute(
                    "SELECT j.job_id FROM registration_mail_jobs j "
                    "LEFT JOIN registration_intents i "
                    "  ON i.mail_job_id = j.job_id "
                    "WHERE j.job_id=%s AND j.purpose='email_verify' "
                    "AND j.expires_at > now() AND j.consumed_at IS NULL "
                    "AND j.status IN ('queued','sent','uncertain','failed') "
                    "AND (i.intent_id IS NULL OR i.completed_at IS NULL) "
                    "FOR UPDATE OF j",
                    (rrow["job_id"],))
                if cur.fetchone() is None:
                    cur.execute(
                        "UPDATE registration_mail_redeliveries SET "
                        "status='cancelled', "
                        "last_error='original_not_sendable' "
                        "WHERE redelivery_id=%s AND status IN "
                        "('queued','failed')",
                        (rrow["redelivery_id"],))
                    _log.warning(
                        "重发投递取消：原验证请求已完成/作废/过期"
                        "（redelivery=%s）", rrow["redelivery_id"])
                    continue
                try:
                    payload = decrypt_payload(rrow["payload_enc"])
                    _send_payload(snd, rrow["email_normalized"], payload)
                except MailSenderUncertainError as exc:
                    # 远端可能已接受：置 uncertain，绝不自动重发（P1-1 同规）
                    cur.execute(
                        "UPDATE registration_mail_redeliveries SET "
                        "status='uncertain', attempts=attempts+1, "
                        "last_error=%s WHERE redelivery_id=%s",
                        (str(exc)[:120], rrow["redelivery_id"]))
                    _log.warning(
                        "重发投递结果不确定（redelivery=%s）：置 uncertain，"
                        "不自动重发", rrow["redelivery_id"])
                    continue
                except MailSenderError as exc:
                    attempts = int(rrow["attempts"] or 0)
                    if attempts + 1 >= _MAX_SEND_ATTEMPTS:
                        cur.execute(
                            "UPDATE registration_mail_redeliveries SET "
                            "status='failed', attempts=attempts+1, "
                            "last_error=%s WHERE redelivery_id=%s",
                            (str(exc)[:120], rrow["redelivery_id"]))
                    else:
                        delay = _RETRY_BACKOFF_BASE_SECONDS * (2 ** attempts)
                        cur.execute(
                            "UPDATE registration_mail_redeliveries SET "
                            "status='failed', attempts=attempts+1, "
                            "last_error=%s, scheduled_at=now() + "
                            "(%s * interval '1 second') "
                            "WHERE redelivery_id=%s",
                            (str(exc)[:120], delay, rrow["redelivery_id"]))
                    _log.warning("重发投递失败（redelivery=%s 第 %d/%d 次）",
                                 rrow["redelivery_id"], attempts + 1,
                                 _MAX_SEND_ATTEMPTS)
                    continue
                cur.execute(
                    "UPDATE registration_mail_redeliveries SET "
                    "status='sent', attempts=attempts+1, sent_at=now(), "
                    "last_error=NULL WHERE redelivery_id=%s",
                    (rrow["redelivery_id"],))
        sent += 1
    return sent


_drain_lock = threading.Lock()


def drain_async():
    """入队后的 best-effort 即时排水（后台守护线程；失败留 queued）。"""
    def _run():
        try:
            drain_once()
        except Exception:
            _log.warning("即时排水失败（作业保持 queued）", exc_info=True)

    if _drain_lock.acquire(blocking=False):
        try:
            threading.Thread(target=_run, daemon=True,
                             name="reg-mail-drain").start()
        finally:
            _drain_lock.release()


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true",
                        help="持续循环（生产 worker）")
    parser.add_argument("--once", action="store_true",
                        help="单次排水后退出（部署钩子/测试）")
    parser.add_argument("--interval", type=int, default=5,
                        help="循环轮询间隔秒（默认 5）")
    args = parser.parse_args(argv)
    if not (args.loop or args.once):
        parser.error("需要 --loop 或 --once 之一")
    if args.once:
        print("drained=%d" % drain_once())
        return 0
    while True:
        try:
            drain_once()
        except Exception:
            _log.exception("worker 循环排水异常（继续）")
        time.sleep(max(1, int(args.interval)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
