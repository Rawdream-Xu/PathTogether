# homePC Admin 插件发布与回滚 Runbook（§16.1 发布底座）

> 本文是 `docs/admin-billing-plugin-implementation-plan.md` §16.1 的**可执行**
> runbook（PR3b 交付的第 1 步：发布脚本 + 文档；**本文不代表任何步骤已执行**）。
> 配套脚本：`scripts/release-plugin-bundle.sh`（在操作者工作站执行，所有远程
> 操作经 `ssh homePC` alias，不硬编码 IP）。
>
> 连接方式（权威）：SSH alias `homePC`（见 `docs/demo-deployment.md` 地址字段
> 三分法——alias 是权威连接方式，公网/解析端点与 LAN 地址只是现状描述）。
>
> 路径约定：
> - 宿主机插件根：`~/svs-viewer-demo-data/plugins`（releases 机制落地后容器以
>   `:ro` 挂载该目录到 `/data/plugins`）；
> - 发布记录：`~/svs-viewer-demo-data/plugins/releases/RELEASE_LOG`（不含密钥）；
> - 备份根：`~/svs-viewer-demo-data/backups/`（目录 0700、文件 0600）；
> - HistoPilot 配置卷：具名 `histopilot-config`（由匿名 volume 迁入，步骤 2）。

每一步都有**验收标准**；任一验收失败 → 停止，按该步的回退指令处理，不要带病
进入下一步。

---

## 步骤 1 —— 发布脚本与 runbook 可审阅（本 PR 交付物）

脚本 `scripts/release-plugin-bundle.sh` 提供五阶段：`stage / preflight / switch /
verify / rollback`，只接受版本化 bundle 目录（`releases/<id>-<version>/`），
**不接受对 live 目录的原地 rsync**；入口切换用临时 symlink + `mv -T`（rename
原子语义）；preflight 校验 hash/pin、不在用、target 在根内、磁盘余量；每次
操作追加不含密钥的 RELEASE_LOG（时间/操作者/版本/hash/旧新 target）。

```sh
# 工作站上审阅（不执行任何远程操作）
bash -n scripts/release-plugin-bundle.sh
scripts/release-plugin-bundle.sh          # 打印用法
```

**验收**：脚本语法通过；用法输出与本文目录布局一致；`grep -n '[0-9]\+\.[0-9]\+\.[0-9]\+\.[0-9]\+' scripts/release-plugin-bundle.sh`
无输出（无硬编码 IP）。

## 步骤 2 —— HistoPilot `/data/config` 匿名卷 → 具名 `histopilot-config`

匿名 volume 不可引用、不可纳入备份清单，必须先迁具名卷。

```sh
ssh homePC '
  set -e
  cid=$(podman ps -q --filter name=histopilot-demo)
  anon=$(podman inspect -f "{{range .Mounts}}{{if eq .Destination \"/data/config\"}}{{.Name}}{{end}}{{end}}" "$cid")
  echo "anonymous config volume: $anon"
  podman volume create histopilot-config
  # 停写：先停容器再复制（sender/agent 不在途）
  podman stop histopilot-demo
  podman run --rm -v "$anon":/from:ro -v histopilot-config:/to alpine \
    sh -c "cp -a /from/. /to/"
  # 校验：文件数与内容 hash 清单一致
  podman run --rm -v "$anon":/from:ro alpine sh -c "cd /from && find . -type f | sort | xargs sha256sum" > /tmp/cfg-from.sha
  podman run --rm -v histopilot-config:/to:ro alpine sh -c "cd /to && find . -type f | sort | xargs sha256sum" > /tmp/cfg-to.sha
  diff /tmp/cfg-from.sha /tmp/cfg-to.sha && echo "COPY OK"
  ls -l /tmp/cfg-from.sha /tmp/cfg-to.sha
'
```

以新卷重建容器（沿用 `~/.config/pt-deploy/*.env` 与原挂载，仅把 `/data/config`
来源换成 `histopilot-config`），再健康检查。

**验收**：`podman inspect histopilot-demo` 显示 `/data/config` 来自
`histopilot-config`；容器 healthz 正常；文件 hash 清单 diff 为空；**旧匿名卷
保留**（步骤 4 恢复演练与保留期结束前不删除——记录 `$anon` 名字到发布笔记）。
**回退**：按原匿名卷重建容器即可（数据未删）。

## 步骤 3 —— 专用备份（PostgreSQL / releases / outbox / config）

现有 `project-sync-backup.timer` 不算本项目备份；建立独立清单：

```sh
ssh homePC '
  set -e
  B=~/svs-viewer-demo-data/backups/admin-release-$(date -u +%Y%m%dT%H%M%SZ)
  install -d -m 0700 "$B"
  # PostgreSQL 逻辑备份（连接参数以实际 pg 容器/端口为准）
  podman exec pg-svs-demo pg_dump -U svs -Fc svs_demo > "$B/pg.dump"
  chmod 0600 "$B/pg.dump"
  # 插件 releases（含 RELEASE_LOG）
  tar -C ~/svs-viewer-demo-data/plugins -czf "$B/plugins-releases.tgz releases RELEASE_LOG 2>/dev/null || \
    tar -C ~/svs-viewer-demo-data/plugins -czf "$B/plugins-releases.tgz releases"
  # usage outbox（pending/dead 文件不丢是 §14.3 门槛）
  tar -C ~ -czf "$B/usage-outbox.tgz svs-viewer-demo-data/sidecar-sessions/usage-outbox" 2>/dev/null || true
  # 具名 HistoPilot config（含 credential → 必须加密 + 保留期）
  podman run --rm -v histopilot-config:/from:ro -v "$B":/to alpine \
    sh -c "cd /from && tar czf /to/histopilot-config.tgz ."
  chmod 0600 "$B"/*
  find "$B" -maxdepth 1 -type f -printf "%f %s bytes\n"
'
# 含 credential 的 config 备份加密（保留期 >= 30 天，密钥离线保存）：
ssh homePC 'gpg --symmetric --cipher-algo AES256 -o "$B/histopilot-config.tgz.gpg" "$B/histopilot-config.tgz" && rm -f "$B/histopilot-config.tgz"'
```

**验收**：备份目录 0700、文件 0600；清单含 `pg.dump / plugins-releases.tgz /
usage-outbox.tgz / histopilot-config.tgz.gpg`；把 `pg.dump -l`（TOC 非空）与
tar 清单打出来人工核对。

## 步骤 4 —— 隔离目录恢复演练

在隔离目录（不碰 live 数据）验证备份可还原：

```sh
ssh homePC '
  set -e
  B=<上一步的备份目录>
  R=~/restore-drill-$(date -u +%Y%m%d%H%M%S); install -d -m 0700 "$R"
  # ① 数据库可还原（恢复到临时库，不动 svs_demo）
  podman exec pg-svs-demo createdb -U svs svs_drill 2>/dev/null || true
  podman exec -i pg-svs-demo pg_restore -U svs -d svs_drill --clean --if-exists < "$B/pg.dump"
  podman exec pg-svs-demo psql -U svs -d svs_drill -tc "select count(*) from billing_accounts"
  podman exec pg-svs-demo dropdb -U svs svs_drill
  # ② outbox：pending/dead 文件数一致
  mkdir -p "$R/outbox" && tar -xzf "$B/usage-outbox.tgz" -C "$R/outbox"
  find "$R/outbox" -type f | wc -l
  # ③ plugin manifest hash 一致
  tar -xzf "$B/plugins-releases.tgz" -C "$R"
  find "$R/releases" -name manifest.json -exec sha256sum {} \;
  # ④ config 权限保持（0600 / 目录 0700）
  gpg --decrypt -o "$R/histopilot-config.tgz" "$B/histopilot-config.tgz.gpg"
  mkdir -p "$R/cfg" && tar -xzf "$R/histopilot-config.tgz" -C "$R/cfg"
  stat -c "%a %n" "$R/cfg" "$R/cfg"/*
  rm -rf "$R"   # 演练目录即弃
'
```

**验收**：① 恢复的临时库可查询且行数>0；② outbox 文件数与备份时一致；
③ releases manifest hash 与备份清单一致；④ config 权限保持 0600/0700。
任一失败 → 修备份再演练，不得进入步骤 5。

## 步骤 5 —— 建立版本化 `plugins/releases` 并迁入现有插件

把现有普通目录 `histopilot` 迁成不可变 release，再原子切换入口；admin bundle
（PR3b 之后）用同一机制：

```sh
ssh homePC '
  set -e
  P=~/svs-viewer-demo-data/plugins; cd "$P"
  install -d releases
  # 现状快照（迁移前证据）
  sha256sum histopilot/manifest.json pathtogether-admin/manifest.json 2>/dev/null || true
'
# 用脚本把仓库 bundle 以版本目录 stage 上去（首版号取当前 manifest pluginVersion）
scripts/release-plugin-bundle.sh stage histopilot <version>
scripts/release-plugin-bundle.sh preflight histopilot <version> --pin <sha256>
scripts/release-plugin-bundle.sh switch histopilot <version>
scripts/release-plugin-bundle.sh verify histopilot <version>
# 旧普通目录重命名保留一个观察期后删除（不再是发布语义的一部分）
ssh homePC 'cd ~/svs-viewer-demo-data/plugins && mv histopilot histopilot.legacy-$(date -u +%Y%m%d) 2>/dev/null || true'
```

**验收**：`ls -l ~/svs-viewer-demo-data/plugins` 显示
`histopilot -> releases/histopilot-<version>`（symlink）；`pathtogether-admin`
同型；旧目录已改名保留；插件加载器（resolve 后校验 target 在根内 + 重算
manifest hash）无告警，Viewer `/` 与 `/plugins/sample-annotator/ui/main.js`
不受影响。

## 步骤 6 —— 容器以 `:ro` 重挂载插件根

发布者只在宿主机 releases 写入；容器内不得修改 bundle：

```sh
ssh homePC '
  set -e
  # 重建 pathtogether-demo：把 -v ~/svs-viewer-demo-data/plugins:/data/plugins
  # 换成 -v ~/svs-viewer-demo-data/plugins:/data/plugins:ro
  # （完整 env/其余挂载沿用 ~/.config/pt-deploy/pathtogether.env 与现有 run 命令，
  #  只改这一处挂载后 podman rm -f + podman run -d，命令历史里有原样模板）
  podman inspect pathtogether-demo --format "{{range .Mounts}}{{.Source}} -> {{.Destination}} {{.Options}}{{println}}{{end}}"
'
```

**验收**：inspect 输出中 `/data/plugins` 的 Options 含 `ro`；容器内写入尝试
被拒（`podman exec pathtogether-demo sh -c 'touch /data/plugins/.w && echo WRITABLE'`
必须失败）；Viewer 与插件 UI 全部正常。

## 步骤 7 —— rootless linger 与重启演练

不能依赖交互 shell 里手工拉起的容器：

```sh
ssh homePC 'loginctl show-user "$USER" -p Linger'          # 期望 Linger=yes
ssh homePC 'systemctl --user list-timers --all | grep -E "pt-|backup"'   # 备份 timer（步骤 3 落 cron/systemd timer）
ssh homePC 'sudo -n systemctl reboot || echo "计划窗口内人工重启"'
# 重启后（经 ssh homePC 复连）：
ssh homePC 'podman ps --filter name=pathtogether-demo --format "{{.Names}} {{.Status}}"'
curl -fsS https://pt.solarise94.fun/healthz
```

**验收**：`Linger=yes`；重启后 `pathtogether-demo` / `histopilot-demo` /
`pg-svs-demo` 自动回到 Up（restart=unless-stopped + linger 生效）；healthz 200；
备份 timer 在列表中且上次运行成功。**未满足先修 linger/systemd unit，不做发布。**

## 步骤 8 —— 发布前置检查（每次发布前重跑）

```sh
# 备份新鲜度（PG 与 outbox/config 备份都应在最近 24h 内）
ssh homePC 'find ~/svs-viewer-demo-data/backups -name pg.dump -mtime -1 -print | grep -q . && echo PG_BACKUP_FRESH'
# 磁盘 / 目标 release 不存在 / manifest+hash / symlink target 在根内 / 容器挂载 RO
scripts/release-plugin-bundle.sh preflight pathtogether-admin <version> --pin <sha256>
ssh homePC 'podman inspect pathtogether-demo --format "{{range .Mounts}}{{if eq .Destination \"/data/plugins\"}}{{.Options}}{{end}}{{end}}" | grep -qw ro && echo PLUGIN_MOUNT_RO'
```

**验收**：三条命令全部通过（脚本任一检查失败即非零退出——此时停止，不 stage
不 switch）。

## 步骤 9 —— 部署向后兼容的 PathTogether migrations / API

先部署只**新增**的迁移与 API（0018/0019 均为 nullable/独立表 forward-fix，
旧代码可与新表共存）；HistoPilot outbox 部署后保持 shadow ingest：

```sh
# 工作站 → 构建镜像 → rsync 代码 → homePC 重建 pathtogether-demo（按 demo-deployment 惯例）
ssh homePC 'cd ~/pathtogether-demo && podman build -t pathtogether-demo:latest . && podman rm -f pathtogether-demo && <原 run 命令>'
curl -fsS https://pt.solarise94.fun/healthz
# 迁移已应用（启动期 ensure_schema 自动执行）
ssh homePC 'podman exec pg-svs-demo psql -U svs -d svs_demo -c "\dt billing_*" -c "\dt ai_usage_events"'
```

**验收**：healthz 200；`billing_accounts / billing_price_books / billing_rates /
ai_usage_events / billing_ledger_entries / provider_balance_snapshots` 表存在；
**部署顺序**：先升 histopilot-demo 再升 pathtogether-demo（run grant 绑定兼容，
见 demo-deployment 2026-08-22 节）。

## 步骤 10 —— 部署 admin bundle + 更新 production pin + 原子切换

pin 是**代码变更**：仓库 `plugins/source-policy.json` 更新为 bundle manifest
的 sha256 → 随平台镜像部署；之后再切 symlink（pin 未更新前 `/admin` 会降级，
这是 fail-closed 预期行为）：

```sh
sha=$(shasum -a 256 plugins/pathtogether-admin/manifest.json | cut -d" " -f1)
# ① 仓库更新 pin（plugins/source-policy.json 的 pathtogether-admin = $sha）并 review
# ② 部署含新 pin 的平台（同步骤 9）
# ③ bundle 走版本化发布：
scripts/release-plugin-bundle.sh stage pathtogether-admin <version>
scripts/release-plugin-bundle.sh preflight pathtogether-admin <version> --pin "$sha"
scripts/release-plugin-bundle.sh switch pathtogether-admin <version>
scripts/release-plugin-bundle.sh verify pathtogether-admin <version>
```

**验收**：`verify` 通过（target/hash/healthz）；`/admin` 渲染宿主页（不再是
`manifest hash mismatch` 降级页）；admin 资产
`/admin/plugin-assets/pathtogether-admin/ui/index.html` owner 200。
**admin bundle 更新绝不直接覆盖正在服务的目录**（只新增 release + 切换）。

### P0-W3（2026-11-11）起来源策略语义：fail-closed

审计 P0-4 修复后 `plugin_source_allowed` 默认拒绝，运维口径变化：

- **未 pin 插件一律 403**（安装与 UI 资产两路径）；策略文件缺失/损坏同样拒绝
  （不再 dev 模式全放行）。放行未 pin 插件的**唯一**开关是策略显式
  `"default": "allow"`（repo 策略不配置该键；仅隔离 dev 环境按需自配）。
- 平台自带 `plugins/sdk`（无 manifest.json）以 `"sdk": null` 显式放行——
  白名单决策，勿当作可复制的先例给第三方插件用。
- **pin 哈希按 LF 行尾计算**：`.gitattributes`（`plugins/** text eol=lf` 等）
  保证任意平台检出 manifest 即 LF；手工计算 `shasum -a 256` 前确认工作区
  文件为 LF（旧 Windows 克隆的 CRLF 工作区会全量失配——重克隆或
  `git checkout -- <file>` 刷新后再算）。
- 新插件上线流程不变：改 `plugins/source-policy.json` 增 pin → 随镜像部署 →
  release 脚本 `--pin` 校验（步骤 10 ①②③）。

## 步骤 11 —— 三身份验证 `/admin`（owner / user / 匿名）+ 重启后复验

| 身份 | 期望 |
|---|---|
| 匿名 | 302 → `/login?next=/admin` |
| 登录 user | 403（无宿主结构泄露，`no-store` + CSP 仍在） |
| 登录 owner（含预览态） | 200 宿主页；**预览态（owner 预览成 user）也必须 403** |

```sh
curl -sI https://pt.solarise94.fun/admin | head -3            # 匿名：302
# owner/user 用浏览器验证（登录 → /admin；owner 再开身份预览访问 /admin 须 403）
# 重启复验：
ssh homePC 'podman restart pathtogether-demo'
curl -fsS https://pt.solarise94.fun/healthz && curl -sI https://pt.solarise94.fun/admin | head -1
```

**验收**：三身份行为与上表一致；重启后 owner 仍能进 `/admin` 且 iframe 握手
正常（概览页双额度卡片——「对话额度」与「金额余额」并列——有数据）。

## 步骤 12 —— 收尾（mywebpage CTA 与软扣费另立项）

mywebpage CTA 修改与软扣费开启**不在本 runbook**（PR4/PR6，且受 §14.3 量化
门槛约束）。本 runbook 终点 = 步骤 11 全绿 + RELEASE_LOG 记录完整。

---

## 回滚路径（§16.2 摘要）

- **Admin UI 故障**：`scripts/release-plugin-bundle.sh rollback pathtogether-admin`
  （切回上一 release；Viewer 不回滚）；或禁用安装行（owner API）→ `/admin`
  降级页，平台其余功能不受影响；
- **HistoPilot 投递故障**：暂停 sender，pending 文件保留；
- **计价故障**：关闭 debit，仅保留 raw usage/unpriced，不删事件；
- **数据库**：forward-fix（新表 nullable 兼容旧代码）；禁止 `git reset --hard`、
  TRUNCATE billing 表或直接改 ledger；
- 发布脚本 rollback 之外的一切数据级回滚以步骤 3/4 的备份为唯一来源。

## 变更记录

- 2026-08-28：初版（PR3b 发布底座第 1 步交付：脚本 + runbook，未执行任何
  远程操作）。
