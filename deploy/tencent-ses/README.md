# 腾讯云 SES 邮件通道

`REGISTRATION_MAIL_SENDER=tencent_ses` 使用官方 Python SDK 的 `SendEmail`，
默认广州 `ap-guangzhou`，发件地址为 `notifications@histopilot.cn`。
队列、加密载荷、一次性验证令牌和现有 SMTP 通道保留，无数据库迁移。

## Review 结论

- 发信域名验证通过还需要创建发件地址、审核模板和 API 权限。SES 默认只允许
  模板发送，不能直接把 SMTP 正文传给 `Simple`。
- 覆盖邮箱验证（中文/英文）、邮箱改绑、注册成功管理员通知、用户反馈。
  模板参数在入队时与原正文一起加密，重发保留冻结入口、语言与原令牌；
  worker 不根据当前 `PUBLIC_BASE_URL` 重建验证链接。
- SES 模板固定 `histopilot.cn` / `histopilot.com` 域名，仅将令牌作为链接变量。
  worker 从冻结链接校验域名、路径和令牌，选择对应域名和语言模板。未知域名、
  非 HTTPS、错误路径和额外查询参数拒绝发送；SMTP 仍保留通用部署能力。
- 模板变量限制为 UTF-8 JSON 800 字节；用户反馈完整正文通过 `feedback.txt`
  附件发送，避免丢失诊断记录。模板使用通用注册名额说明，不把每日上限写死。
- SDK 禁用重试和跨地域熔断；SDK 调试日志强制为 WARNING，避免输出签名头
  和含令牌的模板参数。明确拒绝进入现有有界退避；超时、内部错误、无有效
  MessageId 等进入 `uncertain`，不会自动重发。`sent` 表示 SES 接受请求，
  实际送达还需在 SES 控制台核对。
- 历史载荷不含模板元数据，必须先由 SMTP 排空。不会根据正文猜测用途，
  也不会在 SES 失败时自动改用 SMTP，避免双通道重复发送。

## 配置

主站与独立 mail worker 必须注入同一组配置。下面为本次创建的广州模板 ID，
启用前必须确认全部审核通过。密钥通过现有部署 secret 机制注入，
不写入此文档、Git、命令行参数或日志；子用户登录密码不参与 API 调用。

```dotenv
REGISTRATION_MAIL_SENDER=tencent_ses
REGISTRATION_SES_REGION=ap-guangzhou
REGISTRATION_SES_FROM=notifications@histopilot.cn
REGISTRATION_SES_TEMPLATE_IDS={"email_verify_zh":68617,"email_verify_en":68618,"email_change_zh":68619,"registration_created_zh":68620,"user_feedback_zh":68621,"email_verify_zh_com":68622,"email_verify_en_cn":68623,"email_change_zh_com":68624,"registration_created_zh_com":68625}
```

必需密钥变量：`REGISTRATION_SES_SECRET_ID`、`REGISTRATION_SES_SECRET_KEY`。
可选 `REGISTRATION_SES_TOKEN`（临时凭据）和 `REGISTRATION_SES_REPLY_TO`
（能收信的单个邮箱；不填时 SES 不保证可回复）。不自动复用 COS 密钥。
缺密钥、SDK、任意模板 ID 或非法地域/发件配置会让生产注册前置检查失败。
该检查验证本地配置，不能替代云端权限和模板审核验证。

运行时最小授权为 `ses:SendEmail`，模板管理凭据应与运行时分开。
只读核对可使用 `ListEmailAddress`、`ListEmailTemplates`、`GetEmailIdentity`；
提交模板额外需要 `CreateEmailTemplate`。

| 配置中的模板键 / HTML 文件名 | 固定域名 | API 模板变量 |
| --- | --- | --- |
| `email_verify_zh` | histopilot.cn | `email`, `token` |
| `email_verify_en` | histopilot.com | `email`, `token` |
| `email_verify_zh_com` | histopilot.com | `email`, `token` |
| `email_verify_en_cn` | histopilot.cn | `email`, `token` |
| `email_change_zh` | histopilot.cn | `email`, `token` |
| `email_change_zh_com` | histopilot.com | `email`, `token` |
| `registration_created_zh` | histopilot.cn | `day`, `user_id`, `email`, `count`, `limit` |
| `registration_created_zh_com` | histopilot.com | `day`, `user_id`, `email`, `count`, `limit` |
| `user_feedback_zh` | 无链接 | `feedback_id`（完整正文见附件） |

上传本目录下对应 HTML 为九个独立模板，等待审核通过。静态 HTML 含
`{{变量名}}` 占位符，不能直接作为 app 页面部署。验证模板用于当前 public
注册流程；已退役的 legacy 邀请码流程不支持通过该模板重新启用。

也可用 `python scripts/ses_templates.py` 只读核对模板；加 `--create` 才创建
缺失的模板。脚本默认读取专用 SES 环境变量，也支持
`--credentials-csv /安全路径/腾讯子用户.csv`，仅读取 SecretId/SecretKey。
模板名带内容哈希，重复执行复用同版本模板并核对云端内容，不修改其他模板。
输出仅含模板 ID、审核状态和内容哈希；状态 0=通过、1=待审核、2=拒绝。

2026-11-06 云端核对：发件地址 `notifications@histopilot.cn` 已存在，
`histopilot.cn` 的 MX/SPF/DKIM/DMARC 均验证通过。已提交上述九个独立模板，
创建时状态为 1（待审核）；云端 HTML 结构和变量已回读核对。
早期使用完整 URL 变量的 68612–68616 已停用，不用于配置；审核反馈要求
保留固定域名，现版本按该要求提交。中文/英文与入口域名独立选择，保持
用户在 .cn 选择英文或在 .com 选择中文的能力。
`registration-ses.env.example` 提供无密钥配置，`templates.ap-guangzhou.json`
记录模板 ID 与源内容哈希。该文件中的状态是快照，切换前重新只读查询。

## 切换与回滚

1. 构建包含新 SDK 的镜像；旧的“依赖清单必须一致”快捷构建不能直接用于
   此变更。先保持 `REGISTRATION_MAIL_SENDER=smtp` 部署代码，确认正常发件。
2. 核对发件地址、模板状态全部为通过、子用户权限、广州地域，以及当前
   控制台日额度。截图中的上限为 500 封/天，包含所有后台用途，不能等同于
   本应用每天允许的新注册数。
3. 短暂停止新邮件入队；停止后再复核原队列和重发队列：旧版载荷中的可发
   `queued` / `failed` 作业须通过 SMTP 处理，`uncertain` 只人工核对，
   不自动重置。仍有效的历史验证邮件也可能被用户重发：切换前应等它们
   的 30 分钟有效期结束，或确认不存在缺少模板元数据的有效原作业。
4. 主站和 worker 同步切换 SES 配置并重启，然后恢复入队。保持原有载荷
   密钥和验证哈希盐不变。仅使用用户指定的测试收件邮箱核对中文/英文
   验证、改绑、管理员通知和反馈附件的实际递送与链接。
5. 回滚时将主站与 worker 同步改回 `smtp` 并重启。新载荷仍含完整原始正文，
   旧发送器忽略新增元数据即可发送。保留原 SMTP 配置供回退。

本次改动不自动切换生产服务。

用户已指定私有验收邮箱并要求审核后再发。此次验收的本机后台
检查每五分钟核对全部九个模板的状态和内容；全部通过才发送五封明确标注
“SES 验收测试”的邮件（中文/英文验证、改绑、管理员通知、反馈附件）。
测试使用无对应数据库记录的合成令牌，不创建账号、不修改生产数据。
任何模板退回、内容改变或发送结果异常均暂停发送，不自动重发。
后台检查最多等待七天，依赖本机保持运行；状态、PID 与发送回执保存在
`/mnt/ssd2/tests/tencent-ses-C81zzv/acceptance-state.json`、
`acceptance-watch.pid` 和 `acceptance-watch.log`。这些私有运行文件不进 Git。

## 本地验证（2026-11-06）

SSD2 已核对为 `/dev/nvme1n1p1` 的 ext4 挂载，NVMe 非旋转盘，测试根可写。
运行目录：`/mnt/ssd2/tests/tencent-ses-C81zzv`，运行前设置 TMPDIR/TMP/TEMP，
pytest basetemp、缓存、SDK 安装目标与下载缓存均显式放在该目录。

基础相关回归：**174 passed**，覆盖 SES 真实 SDK 签名/序列化与离线 HTTP、
原始/重发队列、注册防刷、公共注册、邮箱验证、改绑、用户反馈。
日志：`/mnt/ssd2/tests/tencent-ses-C81zzv/pytest-verified.log`。
15 条警告均来自 SDK 使用 Python 已弃用的 `utcfromtimestamp`；无跳过或失败。
工作区 `git diff --check` 通过；已检查本次变更不含附件中的密钥和登录密码。
固定域名修正后 SES 套件 **44 passed**；追加的云端 HTML 属性归一化检查
**1 passed**。对应日志为 `pytest-fixed-domains.log`、`pytest-ses-normalization.log`，
均在同一运行目录。九个云端模板已回读核对 HTML 结构。
实际 SendEmail 由上述审核门控验收执行，最新进度以私有状态文件为准。

官方依据：[SendEmail](https://cloud.tencent.com/document/api/1288/51034)、
[数据结构与模板变量](https://cloud.tencent.com/document/api/1288/51053)、
[模板审核规范](https://cloud.tencent.com/document/faq/1288/52777)、
[SDK 重试配置](https://github.com/TencentCloud/tencentcloud-sdk-python/blob/master/tencentcloud/common/profile/client_profile.py)。

## 生产发布准备（2026-11-06）

- 九个模板现已审核通过；发布前只读回读云端内容与本地 HTML 结构一致。
- 审核门控验收在北京时间 10:32 完成，五封合成邮件均有 SES 递送成功回执。
  合成令牌不对应生产账号；此次发布不另外发送测试邮件。
- 以当前生产 `a16ba162` 集成，无新数据库迁移，管理插件和 HistoPilot 不变。
- 先以 SMTP 启动新镜像，确认新代码、健康检查和旧通道兼容；然后在无活跃
  用户、无在途任务时停机，冻结全部入队方，再检查原队列和重发队列。
  所有可发送或仍可重发的有效载荷必须含模板元数据，否则停止切换并恢复 SMTP。
- SES 激活前在生产数据库隔离副本演练九模板路由、队列成功/不确定状态、
  原 SMTP 对新增元数据的兼容，以及 SMTP 镜像回退。真实 SDK 的 HTTP 出口
  在验收中替换为离线桩，不向实际收件人投递。
- 生产配置同时注入应用与其内置 mail worker；凭据仅通过私有 env 文件传递。
  SDK 采用已验证版本 SES 3.1.135 / common 3.1.203，构建记录 wheel SHA256，
  镜像内运行 `pip check`。
- SES 激活回退到新镜像的 SMTP 就绪容器；整版回退到切换前生产容器。
  两条路径均保留队列载荷密钥、验证盐和 SMTP 配置，绝不自动重发 uncertain。
