# P1-W0 决策记录：LLM 视觉供应商适配层（2026-11-11 开工首日）

## D1 Claude/Gemini：纯 OpenAI 兼容层，不做原生适配器

**决策**：W0 只实现 `openai_compat` 单适配器（`POST {base_url}/chat/completions`，
`Authorization: Bearer`，图像走 `image_url` 的 base64 data URL）。Claude 与
Gemini 通过各自**官方 OpenAI 兼容端点**接入，不写原生协议适配器。

| 供应商 | base_url 模板（预填，owner 可改） |
| --- | --- |
| OpenAI | `https://api.openai.com/v1` |
| DeepSeek | `https://api.deepseek.com/v1` |
| GLM（智谱） | `https://open.bigmodel.cn/api/paas/v4` |
| Qwen（DashScope 兼容模式） | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Moonshot | `https://api.moonshot.cn/v1` |
| Claude（官方 OpenAI 兼容层） | `https://api.anthropic.com/openai/v1` |
| Gemini（官方 OpenAI 兼容层） | `https://generativelanguage.googleapis.com/v1beta/openai` |
| 自建 vLLM | 自填 |

**理由**：

1. Anthropic 与 Google 均官方提供 OpenAI 兼容 chat.completions 端点，且兼容层
   的核心场景就是视觉输入（`image_url` + base64 data URL），无需为 W0 的
   「ROI → 结构化描述」目标引入第二套请求/响应映射。
2. 平台既有 `api_protocol`（openai/anthropic/gemini）属 sidecar（HistoPilot）
   通道；新 `llm_vision` 通道若双协议并行，mock 上游的测试面与错误信封面翻倍，
   W0 工期不划算。
3. `llm_providers.adapter` 列（CHECK 词表 `openai_compat`，默认值）+
   `llm_vision.build_request` 的适配器分派点为原生适配器预留扩展位：后续如
   兼容层出现能力缺口（如 Claude prompt caching / Gemini native thinking），
   加新 adapter 值与新分支即可，无 schema 变更。

**代价（诚实记录）**：兼容层的非标准参数（如 OpenRouter 的路由头、Gemini 的
thinking 配置）W0 不暴露；上游若对兼容层限流/降级，需换 base_url 或等后续
原生适配器。

## D2 密钥与 SSRF

- api_key 复用 `ai_secret.key`（0600 Fernet 密钥文件，app.py
  `_load_or_create_ai_secret` 注入 factory）加密落库；`api_key_encrypted`
  列带 `^enc:` CHECK——**数据库层兜底永不存明文**。Fernet 不可用时新增/换 key
  一律拒绝（fail-closed，比照官方模式门禁）；明文永不回显，只回显尾 4 位
  （`api_key_tail4` 写入时另行计算，列表读取零解密）。
- base_url 仅 owner 可配；scheme 限 http/https；保存时做字面 host 校验，
  **请求时做 DNS 解析复核**（拒绝 localhost/环回/私有/保留/链路本地/组播
  地址）且 `allow_redirects=False`——防 TOCTOU 与重定向绕过。自建内网 vLLM
  场景经环境变量 `LLM_VISION_ALLOW_PRIVATE_UPSTREAM=1` 放开**私网地址**
  （仅私网；环回/localhost 永不放开），由部署运维显式决策。
- 回归测试全部 mock 上游 transport（`FakeResponse`/注入 post 函数），不真实
  外呼；mock DNS（`socket.getaddrinfo`）覆盖 SSRF 负向用例。

## D3 计量

`POST /api/llm/vision` 成功调用后写 `ai_usage_events`（status=`unpriced`、
`unpriced_reason='llm_vision'`）：owner 自配外部供应商没有价格表，定价留空是
诚实口径；token 用量取上游 `usage` 回执（缺失记 NULL）。事件进入既有
admin usage-events 视图（按表读取，无新查询面）。

## D4 前端落点

- W0c：账户设置对话框（`_app_shell.html`）新增 owner 专属「AI 供应商」区块，
  由 `static/llm-provider-panel.js`（`window.HP_LLM_PROVIDERS`，依赖注入）渲染；
  app.js 只加接线。不进 admin.workspace 插件（iframe 桥 + manifest hash pin，
  改动面过大）。
- W0d：工具栏「AI 识图」按钮（`#llm-vision-btn`）由
  `static/llm-vision-ui.js`（`window.HP_LLM_VISION`）装配；取图复用既有
  `/api/slide/<name>/region`；结果进面板内**评论流卡片**（临时卡片，不写
  rois）；share.js 不涉及（按钮仅工作台装配）。
