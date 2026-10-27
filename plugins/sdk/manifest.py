# -*- coding: utf-8 -*-
"""Plugin manifest schema 校验器与版本协商（Stage 5-1，docs §7.0/§7.1）。

纯 stdlib 实现（**不引入 jsonschema 依赖**）：手写最小校验覆盖必填/类型/
semver pattern/permissions 枚举/slots 字符串/provides 能力契约（docs §3）。
供平台 app.py（capabilities 端点 + 加载期协商 + 能力注册表）与示例插件共用，
权威字段定义见 ``plugins/manifest.schema.json``。

版本模型（§7.0）：四个版本字段相互独立，禁止共用一个模糊 ``version`` 字段——
  - ``manifestSchemaVersion``：manifest 文件自身的字段结构（语法不兼容 bump major）；
  - ``pluginContractVersion``：capability API / 领域类型 / 错误码（破坏 API/语义 bump major）；
  - ``bridgeProtocolVersion``：iframe HostBridge 消息协议（消息不兼容 bump major）；
  - ``pluginVersion``：产品/镜像/bundle 版本（正常产品 SemVer，不自动改变其它三者）。

N/N-1 兼容策略：平台接受当前 major 与前一 major（current=1 时 0.x 视为实验期兼容）。
平台在加载/启动前完成版本协商，不兼容则拒绝启动（``PluginVersionError``）。
"""
import re
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# 版本常量（capabilities 端点的单一来源；app.py 复用）
# ---------------------------------------------------------------------------
# 当前 manifest schema major（与 manifestSchemaVersion 的 major 对齐）。
MANIFEST_SCHEMA_MAJOR = 1

# 平台当前支持的 contract / bridge major（§7.2 capabilities 端点 advertised）。
# negotiate_versions 在此基础上额外接受前一 major（N/N-1）。
SUPPORTED_CONTRACT_MAJORS = (1,)
SUPPORTED_BRIDGE_MAJORS = (1,)

# 平台对外声明的完整版本字符串（与 SUPPORTED_*_MAJORS 的 major 一致）。
PLUGIN_CONTRACT_VERSION = "1.0.0"
BRIDGE_PROTOCOL_VERSION = "1.0.0"

# manifest permissions 枚举（§7.1；与 capabilities 列表对齐，未知值校验失败）。
# slide:import（C5 producer 导入合同 §2.1，2026-09-29）：机器生产者导入通道
# 权限——manifest 可申请，但安装时需 admin 显式批准进安装行 approved_scopes
# （未批准的申请 = 安装被拒，fail-closed）；JWT 按安装行裁剪发放，存量安装
# 行不自动获得（防自动提权）。老插件零迁移（不声明则完全不受影响）。
MANIFEST_PERMISSIONS = (
    "slide:metadata:read",
    "slide:region:read",
    "annotation:read",
    "annotation:write",
    "viewer:navigate",
    "slide:import",
)

#: 需要安装时 admin 显式批准的扩展权限（fail-closed：manifest 申请但
#: approvePermissions 未给出 → 安装被拒）。基础 5 项不在此列。
MANIFEST_APPROVAL_REQUIRED_PERMISSIONS = (
    "slide:import",
)

# Manifest v1.1（docs/admin-billing-plugin-implementation-plan.md §8.2/§8.4）：
# 可选 ``adminPermissions`` 数组的枚举——admin 插件申请的管理能力，与
# AdminBridge method→permission 映射表一一对应（app.py 与
# static/admin-host.js 各自持有同源常量，三处不得漂移）。
# 2026-09-03 wave 2（review-2026-09-02-upload-user-limits-admin-ui-cleanup.md
# §4/Batch C5-6/D1）：删除 admin:turn-budgets:read、admin:turn-budgets:write
#（早已无 manifest 消费者的死枚举）、admin:billing:write（人工调账/caps 写
# 入口退役）与 admin:acquisition:read（用户归因退役）；站点访问/Demo 周统计
# 复用 admin:overview:read，不再扩权限域。
# 2026-09-05（review P0 owner 读隔离）：新增 admin:slides:read/write——
# 切片可见性管理（inventory 全量清点 + 显式授权/收回），独立的 slides
# 权限域，不与 users/settings 混用。
# 注意：申请不建立信任——admin 插件信任由 PRIVILEGED_ADMIN_PLUGIN_IDS 白名单 +
# source-policy 显式 sha256 pin + manifest hash 精确匹配 + installation enabled
# 共同判定，永远 fail-closed（app.py _admin_plugin_trusted）。
MANIFEST_ADMIN_PERMISSIONS = (
    "admin:overview:read",
    "admin:users:read",
    "admin:users:write",
    "admin:slides:read",
    "admin:slides:write",
    "admin:invites:read",
    "admin:invites:write",
    "admin:billing:read",
    "admin:audit:read",
    "admin:plugins:read",
    "admin:plugins:write",
    "admin:settings:read",
    "admin:settings:write",
    # 2026-10-27 Credit 钱包管理（spec §9）：赠送/退款/待核对处置/解冻等
    # 资金写动作的独立权限域；读复用 admin:billing:read。旧
    # admin:billing:write 继续退役，不因新增本权限而恢复。
    "admin:credits:write",
)

# semver core：major.minor.patch（不带 prerelease/build，保持 parse 简单；与
# manifest.schema.json 的 pattern 一致）。
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

# ---------------------------------------------------------------------------
# provides 契约常量（插件能力层 docs §3）
# ---------------------------------------------------------------------------
# 能力局部名：小写字母开头，后接小写字母/数字/下划线，总长 2~64。
CAPABILITY_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

# 能力 accessMode 枚举（schema 层）；P1 校验层只放行 read（write 拒绝登记，
# 先跑稳只读链路——docs §6.2）。
CAPABILITY_ACCESS_MODES = ("read", "write")
CAPABILITY_P1_ACCESS_MODES = ("read",)

# requiredPermissions 枚举（有意收窄）：5 项 manifest permissions 中去掉
# viewer:navigate——那是浏览器 UI 侧导航权限，服务端能力无消费场景。
CAPABILITY_REQUIRED_PERMISSIONS = (
    "slide:metadata:read",
    "slide:region:read",
    "annotation:read",
    "annotation:write",
)

# capability description 长度界（给 LLM 看的安全面，docs §3.2）。
CAPABILITY_DESCRIPTION_MIN = 8
CAPABILITY_DESCRIPTION_MAX = 500

# capability 转发超时（毫秒）：缺省 15s，manifest 可声明但不超过 60s。
CAPABILITY_DEFAULT_TIMEOUT_MS = 15000
CAPABILITY_MAX_TIMEOUT_MS = 60000

# 能力级「当前快照上下文」需求（count_cells 方形取景 wire 合同 v1 §1，2026-10-14）：
# 可选字段 ``context``，唯一合法值 ``current_snapshot``——声明后 HP 执行阶段
# 冻结服务端 pending 快照并以签名 snapshot_context 随 dispatch 请求下发（该
# 标记不是模型参数，不进模型可见 schema）；PT dispatch 验签/绑定校验后以
# ``context.snapshot`` 转发给插件。其他任何值校验失败（fail-closed）。
CAPABILITY_CONTEXT_CURRENT_SNAPSHOT = "current_snapshot"

# parameters 允许的 JSON Schema 子集键（与 sidecar TypeBox 校验兼容的公共
# 子集；注册表用同源逻辑拒绝超集，docs §3.2）。2026-10-14：新增布尔
# ``additionalProperties``（count_cells 方形取景合同 v1 §5 要求模型可见
# schema 显式 ``additionalProperties: false``——多余键前后端都拒绝）。
_CAPABILITY_PARAM_KEYS = frozenset(
    ("type", "properties", "required", "enum", "minimum", "maximum",
     "items", "description", "additionalProperties"))
_CAPABILITY_PARAM_TYPES = frozenset(
    ("object", "array", "string", "number", "integer", "boolean", "null"))


class PluginVersionError(Exception):
    """manifest 版本协商不兼容错误。

    属性：
      - ``field``：不兼容的版本字段名（``pluginContractVersion`` /
        ``bridgeProtocolVersion``），或 ``"manifest"`` 表示整体结构非法；
      - ``plugin_version``：插件声明的版本字符串（结构非法时为占位值）；
      - ``supported``：平台接受的 major 列表（N/N-1 范围）。
    """

    def __init__(self, field, plugin_version, supported, message=None):
        self.field = field
        self.plugin_version = plugin_version
        self.supported = list(supported)
        if message is None:
            message = "插件 %s 版本不兼容：插件=%r 平台接受的 major=%r" % (
                field, plugin_version, self.supported)
        super().__init__(message)


def parse_semver(v):
    """解析 semver 字符串 → ``(major, minor, patch)`` 整数三元组。

    非法（非字符串、不匹配 ``major.minor.patch``）抛 ``ValueError``。
    """
    if not isinstance(v, str):
        raise ValueError("版本号需为字符串，got %r" % (v,))
    m = _SEMVER_RE.match(v)
    if not m:
        raise ValueError("非法 semver：%r" % (v,))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _accepted_majors(supported_majors):
    """N/N-1 接受范围：当前 major 与前一 major。

    current=1 时返回 ``[1, 0]``——0.x 视为实验期兼容（前一 major）。注意与
    运行时消息协议（JS BridgeVersion，默认强制同 major）不同：manifest 加载期
    协商更宽松，接受前一 major；运行时每条消息强制同 major。
    """
    current = max(supported_majors)
    return [current, current - 1]


def check_manifest_schema_supported(mv):
    """manifest schema major 是否落在 N/N-1 支持范围。

    ``manifestSchemaVersion.major ∈ {MANIFEST_SCHEMA_MAJOR, MANIFEST_SCHEMA_MAJOR-1}``。
    非法/不可解析的版本号返回 False（调用方应先用 validate_manifest 拦截结构错误）。
    """
    try:
        major, _, _ = parse_semver(mv)
    except (ValueError, TypeError):
        return False
    return major == MANIFEST_SCHEMA_MAJOR or major == MANIFEST_SCHEMA_MAJOR - 1


def _validate_capability_parameters(node, path, errors):
    """递归校验 parameters JSON Schema 子集（docs §3.2）。

    只放行 ``type/properties/required/enum/minimum/maximum/items/description/
    additionalProperties``；``type`` 限 TypeBox 兼容的七种基础类型；
    ``additionalProperties`` 仅接受布尔值（合同上 false = 拒绝模型附加键）。
    发现超集键/类型错/结构错即向 ``errors`` 追加一条（含字段路径），不中断
    遍历。
    """
    if not isinstance(node, dict):
        errors.append("%s 需为对象，got %s" % (path, type(node).__name__))
        return
    for key in node:
        if key not in _CAPABILITY_PARAM_KEYS:
            errors.append("%s.%s 不在 parameters 允许子集内（允许：%s）"
                          % (path, key, ", ".join(sorted(_CAPABILITY_PARAM_KEYS))))
    addl = node.get("additionalProperties")
    if addl is not None and not isinstance(addl, bool):
        errors.append("%s.additionalProperties 需为布尔值（false = 拒绝未"
                      "声明键）" % path)
    t = node.get("type")
    if t is not None:
        if t not in _CAPABILITY_PARAM_TYPES:
            errors.append("%s.type=%r 不在允许类型中（允许：%s）"
                          % (path, t, ", ".join(sorted(_CAPABILITY_PARAM_TYPES))))
    props = node.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            errors.append("%s.properties 需为对象" % path)
        else:
            for pname, sub in props.items():
                _validate_capability_parameters(
                    sub, "%s.properties.%s" % (path, pname), errors)
    req = node.get("required")
    if req is not None:
        if (not isinstance(req, list)
                or not all(isinstance(r, str) for r in req)):
            errors.append("%s.required 需为字符串数组" % path)
        elif isinstance(props, dict):
            for r in req:
                if r not in props:
                    errors.append("%s.required[%r] 未在 properties 中声明" % (path, r))
    enum = node.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            errors.append("%s.enum 需为非空数组" % path)
    for num_key in ("minimum", "maximum"):
        v = node.get(num_key)
        if v is not None and not isinstance(v, (int, float)):
            errors.append("%s.%s 需为数值" % (path, num_key))
    items = node.get("items")
    if items is not None:
        _validate_capability_parameters(items, "%s.items" % path, errors)
    desc = node.get("description")
    if desc is not None and not isinstance(desc, str):
        errors.append("%s.description 需为字符串" % path)


def validate_provides(d):
    """校验 manifest 的可选 ``provides`` 数组（docs §3.1/§3.2），返回错误列表。

    规则（P1）：
      - 不声明 ``provides`` → 空列表（老插件零迁移）；
      - 每项必填 ``name/version/description/parameters/accessMode``，
        可选 ``requiredPermissions/timeout_ms/agent_exposed``，其余键拒绝；
      - name 匹配 ``^[a-z][a-z0-9_]{1,63}$``，同 manifest 内重名拒绝；
      - version 为 semver core；description 8~500 字；
      - parameters 限 JSON Schema 子集（``_validate_capability_parameters``）；
      - accessMode 仅放行 ``read``（``write`` 校验层拒绝，docs §6.2）；
      - requiredPermissions 枚举不含 ``viewer:navigate``；
      - timeout_ms 为 1~60000 的整数（缺省 15000 由消费方补）；
      - agent_exposed 为可选布尔（2026-09-17 P1 缺陷修复新增，additive）：
        缺省/true = 注入官方 AI 读片的 agent 工具集（extra_tools）；false =
        能力级退出——平台网关不再注入，dispatch 端点与登记行不受影响。
        历史安装行缺该字段按暴露处理（app.py _list_agent_capabilities）。
      - context 为可选枚举（2026-10-14 count_cells 方形取景 wire 合同 v1 §1）：
        缺省不声明 = 普通 remote tool（行为不变）；恰为 ``current_snapshot``
        = 该能力需要 HP 执行阶段注入服务端当前快照上下文（签名
        snapshot_context）；其他任何值拒绝登记（fail-closed）。
    """
    errors = []
    provides = d.get("provides")
    if provides is None:
        return errors  # 可选字段：未声明完全不受影响
    if not isinstance(provides, list):
        return ["provides 需为数组，got %s" % type(provides).__name__]
    seen_names = set()
    for i, item in enumerate(provides):
        path = "provides[%d]" % i
        if not isinstance(item, dict):
            errors.append("%s 需为对象，got %s" % (path, type(item).__name__))
            continue
        for key in ("name", "version", "description", "parameters", "accessMode"):
            if item.get(key) is None:
                errors.append("缺少必填字段：%s.%s" % (path, key))
        for key in item:
            if key not in ("name", "version", "description", "parameters",
                           "accessMode", "requiredPermissions", "timeout_ms",
                           "agent_exposed", "context"):
                errors.append("%s.%s 不在 provides 允许字段内" % (path, key))
        name = item.get("name")
        if isinstance(name, str):
            if not CAPABILITY_NAME_RE.match(name):
                errors.append(
                    "%s.name=%r 不匹配 ^[a-z][a-z0-9_]{1,63}$" % (path, name))
            elif name in seen_names:
                errors.append("%s.name=%r 与同 manifest 内其他能力重名"
                              % (path, name))
            else:
                seen_names.add(name)
        elif name is not None:
            errors.append("%s.name 需为字符串" % path)
        version = item.get("version")
        if isinstance(version, str):
            try:
                parse_semver(version)
            except ValueError as e:
                errors.append("%s.version：%s" % (path, e))
        elif version is not None:
            errors.append("%s.version 需为字符串" % path)
        desc = item.get("description")
        if isinstance(desc, str):
            if not (CAPABILITY_DESCRIPTION_MIN <= len(desc) <= CAPABILITY_DESCRIPTION_MAX):
                errors.append("%s.description 长度需在 %d~%d 字之间（当前 %d）"
                              % (path, CAPABILITY_DESCRIPTION_MIN,
                                 CAPABILITY_DESCRIPTION_MAX, len(desc)))
        elif desc is not None:
            errors.append("%s.description 需为字符串" % path)
        params = item.get("parameters")
        if params is not None:
            _validate_capability_parameters(params, "%s.parameters" % path, errors)
        access = item.get("accessMode")
        if access is not None:
            if access not in CAPABILITY_ACCESS_MODES:
                errors.append("%s.accessMode=%r 不在枚举中（允许：%s）"
                              % (path, access, ", ".join(CAPABILITY_ACCESS_MODES)))
            elif access not in CAPABILITY_P1_ACCESS_MODES:
                errors.append("%s.accessMode=%r P1 仅放行 read（write 属 P2）"
                              % (path, access))
        elif access is None and "accessMode" in item:
            errors.append("%s.accessMode 需为字符串" % path)
        perms = item.get("requiredPermissions")
        if perms is not None:
            if not isinstance(perms, list):
                errors.append("%s.requiredPermissions 需为数组" % path)
            else:
                for j, p in enumerate(perms):
                    if not isinstance(p, str):
                        errors.append("%s.requiredPermissions[%d] 需为字符串" % (path, j))
                    elif p not in CAPABILITY_REQUIRED_PERMISSIONS:
                        errors.append(
                            "%s.requiredPermissions[%d]=%r 不在允许枚举中（允许：%s；"
                            "viewer:navigate 是 UI 侧权限，不在列）"
                            % (path, j, p, ", ".join(CAPABILITY_REQUIRED_PERMISSIONS)))
        timeout_ms = item.get("timeout_ms")
        if timeout_ms is not None:
            if (not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool)
                    or not (1 <= timeout_ms <= CAPABILITY_MAX_TIMEOUT_MS)):
                errors.append("%s.timeout_ms 需为 1~%d 的整数"
                              % (path, CAPABILITY_MAX_TIMEOUT_MS))
        agent_exposed = item.get("agent_exposed")
        if agent_exposed is not None and not isinstance(agent_exposed, bool):
            # 可选布尔：缺省/None 视为未声明（= 暴露，与其它可选字段口径一致）
            errors.append("%s.agent_exposed 需为布尔值（缺省 true=暴露给 AI "
                          "agent）" % path)
        ctx = item.get("context")
        if ctx is not None:
            # 可选枚举（wire 合同 v1 §1）：唯一合法值 current_snapshot；
            # 其他值（含空串/未知标记）fail-closed 拒绝登记。
            if ctx != CAPABILITY_CONTEXT_CURRENT_SNAPSHOT:
                errors.append(
                    "%s.context=%r 不在允许枚举中（允许：%r；不声明 = 普通 "
                    "remote tool）" % (path, ctx,
                                       CAPABILITY_CONTEXT_CURRENT_SNAPSHOT))
    return errors


def capability_tool_name(plugin_id, name):
    """注入 agent 的工具名：``{pluginId 去域名点下划线连接}__{name}``。

    如 plugin_id="dev.example.tma"、name="score_core" →
    ``dev_example_tma__score_core``（docs §3.2 命名空间；跨插件天然不冲突）。
    """
    return ("%s__%s" % ((plugin_id or "").replace(".", "_"), name))


def validate_manifest(d):
    """校验 manifest 结构/类型/semver/枚举，返回错误信息列表（**空 = 通过**）。

    纯结构校验，不含版本 major 范围策略（N/N-1 由 check_manifest_schema_supported /
    negotiate_versions 负责）。每条错误信息指明出问题的字段。
    """
    errors = []
    if not isinstance(d, dict):
        return ["manifest 顶层需为对象，got %s" % type(d).__name__]

    # ---- 顶层必填字符串字段 ----
    _STR_FIELDS = ("manifestSchemaVersion", "id", "name", "pluginVersion",
                   "pluginContractVersion", "bridgeProtocolVersion")
    for key in _STR_FIELDS:
        v = d.get(key)
        if v is None or v == "":
            errors.append("缺少必填字段：%s" % key)
        elif not isinstance(v, str):
            errors.append("%s 需为字符串，got %s" % (key, type(v).__name__))

    # ---- 四个版本字段 semver 格式（§7.0，相互独立）----
    for key in ("manifestSchemaVersion", "pluginVersion",
                "pluginContractVersion", "bridgeProtocolVersion"):
        v = d.get(key)
        if isinstance(v, str):
            try:
                parse_semver(v)
            except ValueError as e:
                errors.append("%s：%s" % (key, e))

    # ---- manifestSchemaVersion 结构：必须可解析为 semver（上面已校验） ----

    # ---- ui ----
    ui = d.get("ui")
    if ui is None:
        errors.append("缺少必填字段：ui")
    elif not isinstance(ui, dict):
        errors.append("ui 需为对象，got %s" % type(ui).__name__)
    else:
        entry = ui.get("entry")
        if not isinstance(entry, str) or not entry:
            errors.append("ui.entry 需为非空字符串")
        slots = ui.get("slots")
        if slots is None:
            errors.append("缺少必填字段：ui.slots")
        elif not isinstance(slots, list):
            errors.append("ui.slots 需为数组，got %s" % type(slots).__name__)
        elif not slots:
            errors.append("ui.slots 不能为空")
        else:
            for i, s in enumerate(slots):
                if not isinstance(s, str) or not s:
                    errors.append("ui.slots[%d] 需为非空字符串" % i)
        # ui.fileHashes（可选，manifestSchemaVersion 1.1 起支持）：bundle 内容
        # 完整性绑定——{相对 bundle 根的路径: sha256(64hex)}。特权 admin 插件
        # 依赖它把 manifest pin 间接绑定到全部可服务文件（2026-08-29 复核：
        # 此前 pin 只锁 manifest.json，UI 文件可漂移）。
        fh = ui.get("fileHashes")
        if fh is not None:
            if not isinstance(fh, dict) or not fh:
                errors.append("ui.fileHashes 需为非空的路径→sha256 对象")
            else:
                for path, digest in fh.items():
                    if not isinstance(path, str) or not path or \
                            path.startswith("/") or ".." in path.split("/") or \
                            "\\" in path:
                        errors.append("ui.fileHashes 键 %r 需为相对 bundle 根的"
                                      "安全路径" % path)
                    if not isinstance(digest, str) or                             not re.fullmatch(r"[0-9a-fA-F]{64}", digest or ""):
                        errors.append("ui.fileHashes[%r] 需为 64 位 sha256 hex"
                                      % path)

    # ---- service ----
    # baseUrl 的 http/https 约束只对**声明了 provides** 的 manifest 生效：
    # 平台仅向提供能力（dispatch 会转发）的地址出站；纯 UI 插件（如
    # sample-annotator）的 baseUrl 是同源占位（"/"），不作 out-of-band 请求。
    _has_provides = isinstance(d.get("provides"), list) and bool(d.get("provides"))
    svc = d.get("service")
    if svc is None:
        errors.append("缺少必填字段：service")
    elif not isinstance(svc, dict):
        errors.append("service 需为对象，got %s" % type(svc).__name__)
    else:
        base = svc.get("baseUrl")
        if not isinstance(base, str) or not base:
            errors.append("service.baseUrl 需为非空字符串")
        elif _has_provides and urlparse(base).scheme not in ("http", "https"):
            # 平台 dispatch 会向该地址发起出站请求：非 http(s)（file/gopher/
            # 无 scheme 等）一律拒绝登记（D1 SSRF 收口的第一道闸）。
            errors.append("service.baseUrl 仅允许 http/https 绝对地址，got %r" % base)
        health = svc.get("health")
        if not isinstance(health, str) or not health:
            errors.append("service.health 需为非空字符串")

    # ---- permissions 枚举 ----
    perms = d.get("permissions")
    if perms is None:
        errors.append("缺少必填字段：permissions")
    elif not isinstance(perms, list):
        errors.append("permissions 需为数组，got %s" % type(perms).__name__)
    else:
        for i, p in enumerate(perms):
            if not isinstance(p, str):
                errors.append("permissions[%d] 需为字符串，got %s" % (i, type(p).__name__))
            elif p not in MANIFEST_PERMISSIONS:
                errors.append("permissions[%d]=%r 不在允许枚举中（允许：%s）"
                              % (i, p, ", ".join(MANIFEST_PERMISSIONS)))

    # ---- adminPermissions（可选，Manifest v1.1，docs §8.2）----
    # 未声明 → 完全不受影响（旧 manifest 零迁移）；声明则必须是数组且逐项落在
    # 11 项管理枚举内。admin 权限与普通 permissions 分开声明，避免 viewer 插件
    # 语义混杂；申请本身不建立信任（见 MANIFEST_ADMIN_PERMISSIONS 注释）。
    admin_perms = d.get("adminPermissions")
    if admin_perms is not None:
        if not isinstance(admin_perms, list):
            errors.append("adminPermissions 需为数组，got %s"
                          % type(admin_perms).__name__)
        else:
            for i, p in enumerate(admin_perms):
                if not isinstance(p, str):
                    errors.append("adminPermissions[%d] 需为字符串，got %s"
                                  % (i, type(p).__name__))
                elif p not in MANIFEST_ADMIN_PERMISSIONS:
                    errors.append(
                        "adminPermissions[%d]=%r 不在允许枚举中（允许：%s）"
                        % (i, p, ", ".join(MANIFEST_ADMIN_PERMISSIONS)))

    # ---- provides（可选：插件对外提供的服务端能力，docs §3）----
    errors.extend(validate_provides(d))

    return errors


def negotiate_versions(manifest):
    """contract/bridge major N/N-1 协商。

    先 ``validate_manifest``（结构非法 → 抛 ``PluginVersionError(field="manifest")``），
    再检查 ``pluginContractVersion.major`` 与 ``bridgeProtocolVersion.major`` 落在
    N/N-1 接受范围；不兼容抛 ``PluginVersionError``（field 指明哪个版本字段，
    message 含插件值与平台接受范围）。

    成功返回协商结果 dict：echo 插件实际声明并协商成功的版本（§7.0 要求
    ``plugin_contract_version`` 记录运行时实际协商成功的值，不记产品版本）。
    """
    errors = validate_manifest(manifest)
    if errors:
        raise PluginVersionError("manifest", "(malformed)", [],
                                 message="manifest 校验失败：" + "; ".join(errors))
    contract = manifest["pluginContractVersion"]
    bridge = manifest["bridgeProtocolVersion"]
    cmaj = parse_semver(contract)[0]
    bmaj = parse_semver(bridge)[0]

    contract_accept = _accepted_majors(SUPPORTED_CONTRACT_MAJORS)
    bridge_accept = _accepted_majors(SUPPORTED_BRIDGE_MAJORS)
    if cmaj not in contract_accept:
        raise PluginVersionError("pluginContractVersion", contract, contract_accept)
    if bmaj not in bridge_accept:
        raise PluginVersionError("bridgeProtocolVersion", bridge, bridge_accept)

    return {
        "manifestSchemaVersion": manifest["manifestSchemaVersion"],
        "id": manifest["id"],
        "pluginVersion": manifest["pluginVersion"],
        "pluginContractVersion": contract,
        "bridgeProtocolVersion": bridge,
    }
