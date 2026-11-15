# P1-W1 标注类别体系 —— 决策记录

> 2026-11-12 ｜ 分支 `wp/p1-w1`（自 platform `fc4787a2` 切出）
> 依据：《P0-P4工作任务流.md》〇 通用工作规约 + P1-W0 节类别体系条目；《全局战略计划.md》§4.2/§5.2（互操作契约：GeoJSON 双向 QuPath/DSA）；migration 0095 重编前例。

## D1 类别配置：独立 migration 表 `label_classes`（否决 settings_store 方案）

**结论**：新建 `migrations/0096_label_classes.sql` 独立表；否决 `platform_settings` JSONB 方案。

| 维度 | 独立表 `label_classes` | settings_store 单键 JSONB |
|---|---|---|
| 并发写 | 行级 `FOR UPDATE` + revision CAS（0095 同款），两人同时建类别不互踩 | 单键整体覆盖，CAS 粒度=整个类别清单，后写覆盖先写 |
| 唯一性 | `name` UNIQUE 数据库层兜底 | 应用层判重，并发窗口可重名 |
| 排序/启用/改色 | `sort_order`/`enabled`/`color` 列级 UPDATE + revision 递增，审计到字段 | 整段 JSONB 重写，diff 不可读 |
| 下游消费（P2-W2 数据集快照按类别直方图 / P1-W4 分割回写按类别） | SQL `JOIN/GROUP BY` 直查 | 读键后应用层解析 |
| 与平台一致性 | 与 llm_providers（0095）/rois 同构：migration + `*_store.py` + JSONB/列 + CAS | settings_store 现仅承载标量配置（registration_mode / ai_safety），无清单型先例 |

settings_store 被否决的决定性理由：类别清单是**多人并发编辑的结构化业务数据**（病理顾问建类别 ↔ 医生标注同时进行），不是进程配置；P2 训练快照要按类别做 SQL 聚合。两者长期都会存在，但类别不属于前者。

## D2 语义模型：classification 存 rois 权威 dict（JSONB 内，不新列）

- `rois.data.classification = {"name": str, "color": [r,g,b]}`；**name 是权威**，color 是**展示快照**（QuPath 导入后按 classification.color 着色，不认 properties.color）。
- 不在 rois 加离散列：现有 `_GEOM_KEYS` 白名单/镜像列全部是几何字段；classification 是纯载荷，JSONB 内即可满足（任务书明示「JSONB 内不加列」）。按 name 查标注属 P2 需求，届时用 `data->>'classification'` 表达式索引，不动 schema。
- 标注改绑类别、类别改色：标注侧 color 快照**不回填**（`revision` 不 bump、change_log 无事件）；导出时由 `annotation_export.classification_of` 用**当前类别色**覆盖快照——QuPath 着色恒等于类别当前色，历史快照只兜「类别已删」的降级。
- 合法性：写入侧**引用校验**（name 必须命中启用中的类别），非外键——类别删除不禁用历史标注（见 D4）。
- 「无类别」是合法态：`classification: null`（存量标注与不选类别的新标注）；一旦落 dict 则 name 非空。

## D3 权限与错误码

- 类别管理（CRUD）**仅 owner**（403 `forbidden`）：类别是全局语义体系（黏膜/平滑肌/血管/神经），由管理员治理；普通用户只读启用类别。
- 标注携带类别：owner+user 可用（创建/更新走既有 `_resolve_anno_by_id(require_write=True)` 鉴权）；guest 被既有会话闸拦截。
- 稳定错误码（信封 `{error, code, retryable?, details?}`，规约 #3）：
  - `unknown_class` 400 —— 标注创建/更新携带不存在/已停用/已删除的类别 name；
  - `invalid_payload` 400 —— classification 形状非法（缺 name / color 非整数三元组）；
  - `duplicate_name` 400 —— 类别名重复；
  - `not_found` 404 / `revision_conflict` 409 —— 与 0095 同语义。

## D4 类别删除语义：软删除（fail-safe，不禁历史）

`DELETE` 置 `deleted=true`（行保留）：历史标注的 classification 快照原样保留、导出降级用快照色；name 唯一约束需配合「删除后允许重建同名」→ 唯一索引改为部分索引 `WHERE NOT deleted`（新启用类别不可与存续类别重名；重建同名类别获得新色后，旧标注快照不变——可接受：QuPath 打开历史导出仍按快照色显示）。硬删除会制造悬空引用 + 破坏审计，否决。

## D5 GeoJSON 导出（QuPath 0.7 互操作验收口径）

`annotation_export.py` 已有 classification 字段位（现由 label 哈希色派生，app.js `labelColor` 同式）。本包改为：

```
classification_of(item, raw):
  1. raw/classification 为 dict 且 name 非空 → {name, color: 当前类别色}
  2. 类别表查无此 name → 快照 color（整型三元组校验后）→ 退化 label 哈希色
  3. 无 classification → 旧行为：{name: label（":"→"："）, color: label 哈希色}
```

properties.classification 恒存在（QuPath 按 classification.name 分组着色）；metadata/扁平字段新增 `classification_name`。纯函数签名不变（`build_feature(item, raw)`），store 查询注入在 `annotation_export` 模块级可替换钩子 `_class_color_lookup` 上（测试 monkeypatch；生产由 store 模块注入，避免 app 依赖）。

## D6 前端：static/anno-classes.js（HP_* 依赖注入）+ share.js 不同步

- `HP_AnnoClasses.mount({select, legend, apiFetch, i18n, onChange})`：拉 `/api/label-classes?enabled=1` → 填充类别下拉（标注选项 popover 内）+ 颜色图例；`activeClass()` 供 `submitAnnotationDraft` 组装 body；类别数据缓存与失效由模块自持。
- 无类别配置时下拉回落「无类别」单选项（优雅降级，不阻断绘制）。
- **share.js 审计结论（2026-11-12，platform `fc4787a2`）**：share.js 无标注创建/编辑写路径（无 `add_roi` / `POST /api/annotation` 调用；仅有评论 POST 与渲染），标注面板/绘制工具为工作台（index.html）专属，`anno-classes.js` 仅进 index.html → **无需同步**。share.js 渲染色板仍走 `labelColor(label)` 哈希；类别色渲染若未来进分享页，再评估（P5）。
- app.js 只加接线：`els` 注册、`HP_AnnoClasses.mount(...)`、`submitAnnotationDraft` 读 activeClass 塞进 body、PATCH 编辑路径带 `classification`——不增绘制/查看器逻辑。

## D7 测试矩阵（回归先行）

| 层 | 文件 | 钉住 |
|---|---|---|
| store | tests/test_label_class_store.py | CRUD/唯一名/CAS/软删/审计/owner scope 无关性 |
| http | tests/test_label_class_http.py | 鉴权（未登录401/user写403/owner通）、错误码、public 视图无内部字段 |
| 标注链路 | tests/test_annotation_classification.py | 创建携带类别落 JSONB、PATCH 改绑/清空、非法类别 `unknown_class`、导出 classification（含类别已删降级/无类别旧行为）、change_seq 不因改色漂移 |
| JS | tests/js/anno-classes.test.ts | pickActiveClass 降级、legend 渲染、payload 组装 |

验收：4 类（黏膜/平滑肌/血管/神经 + 手动增加）→ 标注 → 导出 GeoJSON 在 QuPath 0.7 打开分类正确（人工抽查，任务书验收项）。

## 遗留

- 项目级类别作用域（当前全局）——病理顾问给出分项目体系需求后再演进（表加 project 列即可，不阻塞）。
- P1-W2 点标注 / P1-W4 AI 回写携带类别：写路径同参数（`classification={"name": ...}`），无需新 migration。
