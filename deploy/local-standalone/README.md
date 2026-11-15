# PathTogether 本地部署说明

> 部署日期：2026-11-10 ｜ 单机本地部署（WSL2 Ubuntu-22.04 + 内嵌 PostgreSQL + gunicorn）

## 架构

```
Windows 桌面按钮「PathTogether 一键启动」
   └─ wsl.exe → Ubuntu-22.04
        ├─ PostgreSQL 15  (内嵌二进制, 端口 5433, 数据 ~/pt-pgdata, 无需 sudo)
        ├─ gunicorn (2 worker × 8 线程, 0.0.0.0:8000, share_server:combined_app)
        │     └─ 应用代码 ~/pt-app（main 分支 b11eeb7, LF 检出）
        └─ 数据目录 ~/pt-data/{uploads, share, plugins, secrets, logs}
Windows 浏览器 http://localhost:8000  （WSL2 localhost 自动转发）
```

## 访问凭证

- 地址：http://localhost:8000（工作台 /app，分享页 /s/<token>）
- 账号：见 `~/pt-data/secrets/owner_password`（首次部署自动生成；本地部署
  主账号以库内启用账号为准——2026-11-15 时为 user01，bootstrap 的 owner
  登录行已停用）
- 密码：**永不入库**——查看：WSL 内 `cat ~/pt-data/secrets/owner_password`
- AI 对话功能（sidecar）显示 unreachable 属预期（HistoPilot 未部署，优雅降级，不影响阅片/标注/插件）

## 日常操作

| 操作 | 方式 |
|---|---|
| 启动 | 桌面「PathTogether 一键启动」或运行 `本地部署\PathTogether一键启动.bat`（启动后自动开浏览器） |
| 停止 | 桌面「PathTogether 停止」 |
| 建号 | `本地部署\本地建号.bat`（或见下方「本地建号」） |
| 状态 | WSL: `bash 本地部署/pt-app-ctl.sh status` |
| 日志 | WSL: `tail -f ~/pt-data/logs/error.log`（访问日志 access.log） |
| 数据库 | WSL: `~/pt-venv/.../pginstall/bin/psql -p 5433 -U pt pathtogether` |

## 本地建号（2026-11-10 增补）

**为什么需要**：平台自助注册是 fail-closed 安全设计（public 模式前置 HTTPS 入口 +
Secure Cookie + 生产级 SMTP 邮箱验证 + 双协议文稿发布，任一缺失自动降级 closed；
管理台手动建号端点已退役 410，`useradmin` CLI 仅重置 owner 密码）。本地 HTTP 部署
没有 Web 侧建号入口，故提供主机侧脚本。

**脚本**：`create_local_user.py` —— 复用平台自身的
`user_store_pg.create_user_with_total_allowance`（同源 pbkdf2 哈希；单事务内建
用户 + 总额度 + credits 钱包 + `user.create` 审计，不绕过平台不变量），即建即
active 可登录。

| 用法 | 命令 |
|---|---|
| Windows 双击 | `本地部署\本地建号.bat`（提示输入登录名 + 两次密码） |
| WSL 命令行 | `~/pt-venv/bin/python /mnt/f/切片三维重建软件平台构建/本地部署/create_local_user.py <登录名> --password-stdin` |

- 密码策略 15..200 字符（stdin/文件/交互三选一，绝不走命令行明文参数）
- 可选：`--display-name 名字`、`--no-ai-access`、`--limit-nano-cny N`（缺省额度 = 平台全局默认，当前 ¥20）
- 需要平台已启动（连本地 PG 5433）；审计 actor 记为当前 owner

## 升级应用代码

```bash
# WSL 内：
cd ~/pt-app && git fetch /mnt/f/切片三维重建软件平台构建/PathTogether <分支> && git reset --hard FETCH_HEAD
bash /mnt/f/切片三维重建软件平台构建/本地部署/pt-app-ctl.sh restart
```

## 上传/存储限制生效值（2026-11-15 解禁）

本地部署上传闸门由 `pt-app-ctl.sh load_env` 注入 env 放宽（env 为应用
import 期读取，**改动须 restart 生效**）；配额体系/审计语义不变。

| 闸门 | 平台默认 | 本地部署生效值 | 定义位置 |
|---|---|---|---|
| 单文件上限 | 10 GiB | **50 GiB**（`UPLOAD_MAX_REQUEST_BYTES`） | upload_guard.py:71 |
| 每身份并行摄取 | 1（串行） | **2**（`COS_MAX_ACTIVE_UPLOADS_PER_IDENTITY`） | cos_config.py:40 |
| 全局并行摄取 | 2 | **4**（`COS_MAX_ACTIVE_UPLOADS_GLOBAL`） | cos_config.py:42 |
| 每小时上传创建 | 60 | **600**（`UPLOAD_HOURLY_REQUEST_LIMIT`） | upload_guard.py:101 |
| 用户总配额 | 10 GiB 默认行 | **user01 = 1 TiB**（admin API 硬边界） | upload_guard.py:84-85 |
| 磁盘保留水位 | 20 GiB | 20 GiB（**保留不动**，物理保护） | upload_guard.py:93 |

- **总配额调整**：`set_owner_storage_quota.py <login_id>...`（复用平台
  `upload_guard.set_user_quota`，行锁 + 同事务审计，不绕不变量）。
  2026-11-15 已把 `user01` 从 10 GiB 调到 1 TiB；`owner` 登录行已停用
  （disabled=t），未调整。磁盘 1007 GiB、余 878 GiB——磁盘物理量先于
  配额触顶。
- **批量导入节奏**：`ingestion_jobs_one_waiting_per_owner` DB 唯一索引
  （0066）不受 env 影响——同一时刻至多 活跃2+等待1=3 个任务在系统内，
  第 4 个创建请求 409 `cos_waiting_limit`。大批量（如 125 片序列）请
  分批导入：每批 ≤3 个连续创建，待前面的发布后再导下一批。

## 文件清单

| 文件 | 用途 |
|---|---|
| pg-ctl.sh | PG 初始化/启停（init 仅首次） |
| pt-app-ctl.sh | 应用启停/状态（自动带起 PG；load_env 含上传解禁 env） |
| create_local_user.py | 主机侧建号（方案 B，见上节） |
| set_owner_storage_quota.py | 主机侧存储配额调整（复用 set_user_quota，见上节） |
| 本地建号.bat | 建号的 Windows 双击入口 |
| PathTogether一键启动.bat / PathTogether停止.bat | Windows 入口（桌面快捷方式指向它们） |

## 已知边界

- 代码 POSIX-only：只能在 WSL/容器运行（fcntl），Windows 侧仅作浏览器/编辑器宿主
- WSL 内路径必须 LF（脚本已按 LF 提交；升级代码用上述 git 方式可保 LF）
- 上传的切片存储在 WSL 文件系统 `~/pt-data/uploads`（ext4 性能优于挂载 /mnt/f）
