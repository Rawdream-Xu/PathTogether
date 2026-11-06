# SES / SMTP 按用途路由

用户 2026-11-06 确认：SES 只处理验证码和常规通知；格式兼容申请与 Bug /
用户反馈继续使用 SMTP。`REGISTRATION_MAIL_SENDER=tencent_ses` 启用这套
混合路由；`smtp` 保留原有全部走 SMTP 的行为，供分步发布与回退。

| 邮件用途 | 通道 | 内容 |
| --- | --- | --- |
| 邮箱验证（中英文、.cn/.com） | SES | 四个审核模板，冻结的入口、语言和令牌 |
| 邮箱改绑 | SES | .cn/.com 两个审核模板 |
| 注册成功管理员通知 | SES | .cn/.com 两个审核模板 |
| 格式兼容申请 | SMTP | 原始完整正文，含设备信息、诊断、样本元数据与下载信息 |
| Bug / 用户反馈 | SMTP | 原始完整问题描述和诊断正文 |

`PurposeRoutingMailSender` 只按服务端冻结的 `template_key` 选择通道；未知
用途拒绝发送。SES/SMTP 任一失败或结果不确定都不会切换另一个通道。
原队列、重发、租约和有界重试保留；uncertain 永不自动重发。两个生产
调用方（registration_mail_worker、format_request_store）统一经过
`_send_payload()`；fake/自定义旧发送器继续使用原 send 接口。

## 配置

主站与内置 mail worker 使用同一组配置。SES 模式同时检查 SMTP 与 SES
配置；缺少其中任何一个通道的必要配置均 fail closed。保留原 SMTP 的
主机、账号、授权码、From、端口和 SSL/STARTTLS 设置，不自动复用 COS 密钥。

```dotenv
REGISTRATION_MAIL_SENDER=tencent_ses
REGISTRATION_SES_REGION=ap-guangzhou
REGISTRATION_SES_FROM=notifications@histopilot.cn
REGISTRATION_SES_TEMPLATE_IDS={"email_verify_zh":68617,"email_verify_en":68618,"email_change_zh":68619,"registration_created_zh":68620,"email_verify_zh_com":68622,"email_verify_en_cn":68623,"email_change_zh_com":68624,"registration_created_zh_com":68625}
```

SES 必需密钥：`REGISTRATION_SES_SECRET_ID`、`REGISTRATION_SES_SECRET_KEY`；
可选 `REGISTRATION_SES_TOKEN` 和 `REGISTRATION_SES_REPLY_TO`。密钥只通过
私有 secret 配置注入，不写入代码、Git、命令行或日志。运行时最小权限为
`ses:SendEmail`，模板管理凭据应与运行时分开。

八个固定域名模板保留中文/英文与入口域名的独立选择。参数 JSON 不超过
800 UTF-8 字节，只把令牌作为链接变量。未知域名、非 HTTPS、错误路径、
重复或额外查询参数拒绝发送，不根据当前 PUBLIC_BASE_URL 重建旧链接。

SDK 固定为已验证版本 SES 3.1.135 / common 3.1.203；禁用 SDK 重试与跨地域
熔断。SDK 日志为 WARNING，异常只输出稳定类别。返回 MessageId 代表接受，
实际送达需另查 SES 记录。既有五封审核门控验收邮件已送达且用户确认收到；
本次路由调整不额外发送真实邮件。

`templates.ap-guangzhou.json` 是八模板 ID、内容哈希及审核状态的查询快照；
切换前运行 `python scripts/ses_templates.py` 重新只读核对。`--create` 仅在
需要创建缺失版本时使用。此前创建的复杂通知模板 68621、68845 不在配置或
发送白名单中；云端未执行删除。

## 切换与回退

1. 基于实际生产修订整合、回归，构建带 SDK 的镜像并校验完整运行树及依赖。
   本次没有数据库迁移，不更改 HistoPilot、支付配置或管理插件。
2. 在生产数据库的隔离副本上验证八个 SES 路由、两个 SMTP 路由、原队列与
   重发、uncertain 不重发，以及实际旧镜像读取新载荷后走 SMTP 的兼容性。
   SES HTTP 与 SMTP 发送均使用离线桩，不向任何实际收件人投递。
3. 新镜像先以 `smtp` 启动；无活跃用户或在途任务时才切换。然后停机冻结
   全部入队方，再检查原队列、重发队列与仍有效的历史验证邮件。缺少用途
   元数据或出现需人工核对的不确定状态时，停止 SES 激活，恢复 SMTP。
4. 主站和内置 worker 同步启用混合路由，保留原载荷密钥、验证盐与 SMTP
   配置。上线后只检查健康、实际发送器路由、队列状态与两个正式域名。
5. SES 激活失败时恢复新代码的 SMTP 就绪容器；整版回退时恢复原生产容器。
   新载荷仍含完整 subject/body，旧 worker 可忽略元数据；回退不恢复数据库，
   不重置 sent/uncertain，也不自动再次发送。

## 验证依据

此前发现格式申请 worker 直接调用旧 send 接口；六个隔离回归在旧代码上
全部失败，已统一适配发送边界。最终路由测试覆盖常规 SES、复杂 SMTP、
完整原文、未知用途拒绝、两种通道错误均不跨通道回退，以及队列不确定态。
所有测试使用独立 PostgreSQL 和离线邮件桩，产物放在 /mnt/ssd2/tests。

官方依据：[SendEmail](https://cloud.tencent.com/document/api/1288/51034)、
[模板审核规范](https://cloud.tencent.com/document/faq/1288/52777)。
