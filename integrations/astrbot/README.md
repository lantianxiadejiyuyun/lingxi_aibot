# AstrBot 接入灵犀

AstrBot 连接聊天平台，灵犀继续负责模型、人设、记忆、日程、待办和工具调用。插件只处理明确绑定的私聊，并停止 AstrBot 对这条消息的默认 AI 回复，避免两个 AI 同时回答。

## 1. 启动 AstrBot

从灵犀仓库根目录执行：

```sh
docker compose -f docker-compose.astrbot.yml --profile astrbot up -d
```

固定使用 `soulter/astrbot:v4.29.1`。数据放在 `data/astrbot`，本目录自动挂载为插件。管理界面默认仅监听主机 `127.0.0.1:6185`；远程服务器可通过 SSH 转发到自己电脑后打开 `http://127.0.0.1:6185`：

```sh
ssh -L 6185:127.0.0.1:6185 your-user@your-server
```

在 AstrBot 后台完成初始账号设置，连接所需的平台，再在插件页启用「灵犀 AI 助理桥接」。此配置没有开放 OneBot 端口；需要 OneBot 时按 AstrBot 官方平台文档补充对应设置。

已有 AstrBot 时，把本目录完整复制到它的 `data/plugins/astrbot_plugin_lingxi/` 并重载插件即可。**不要把整个灵犀仓库当成单插件通过 URL 安装**；更新时同步本目录并重载，挂载安装则拉取灵犀代码后重启 AstrBot。

如和仓库主编排共用 `aibot` 网络，可使用：

```sh
docker compose -f docker-compose.yml -f docker-compose.astrbot.yml --profile astrbot up -d astrbot
```

这种方式 API 地址可填 `http://app:8000/lingxi/api/v1`。独立编排时填灵犀实际可达的局域网地址或 HTTPS 地址，不能把容器里的 `127.0.0.1` 当成宿主机。`/lingxi` 应替换为你的后台入口；若没有入口前缀则直接使用 `/api/v1`。

## 2. 绑定自己的私聊

1. 在灵犀「设置 → 账号」生成当前用户的 API Token。
2. 向 AstrBot **私聊**发送 `/lingxi 身份`，复制返回的完整私聊标识。
3. 在 AstrBot 插件配置添加一个账号组，填写 API 基地址、Token、允许的私聊标识；保存并重载插件。Token 只填后台，不要通过聊天发送。
4. 在同一个私聊发送 `/lingxi 绑定`，然后即可直接聊天，例如「列出明天的日程」或「添加待办：周五提交方案」。

支持多个灵犀账号配置组，各组使用自己的用户 Token 和私聊标识。一个灵犀账号仅绑定一个私聊；绑定另一个私聊时会取消旧目标的未发送队列。插件不接受群聊，未被配置允许的私聊也无法绑定。

| 命令 | 作用 |
|---|---|
| `/lingxi 身份` | 获取当前平台实例和私聊标识 |
| `/lingxi 绑定` | 明确绑定当前私聊；原会话被删除时重建会话 |
| `/lingxi 状态` | 查看绑定情况 |
| `/lingxi 解绑` | 停止当前私聊的灵犀桥接和主动推送 |
| `/help`、`/model` 等 | 绑定后交给灵犀原有命令处理 |

Token 撤销或轮换后，在插件后台更新 Token，再检查绑定状态。AstrBot 的 `secret` 字段仅遮蔽后台输入框，配置文件仍保存原值；备份插件配置时同样需要保护它。

## 3. 主动消息

在灵犀配置通知渠道 `astrbot`，并按需启用主动陪伴或简报。插件默认每 15 秒拉取当前账号的队列，只投递到已明确允许的同一个私聊。

投递成功才确认；发送失败交由灵犀退避重试，最多 5 次、最长 24 小时。长消息按每段最多 1800 字符发送，每完成一段就保存进度，断线后从下一段继续；总消息支持 12000 字符。确认请求丢失时只重试确认，不重复发送已记录的分段。

平台发送与本地进度写入无法组成一个事务，因此进程恰好在两步之间退出仍可能重复一段。不要让两个 AstrBot 实例同时使用同一灵犀账号。插件保存最近 512 个平台消息 ID 防止重投递；普通 AI 对话失败或超时**不会自动重发**，以免重复创建任务等操作。平台没有消息 ID 时无法提供该层去重。

## 当前范围

- 支持文字聊天、灵犀的多轮工具调用、日程/待办及主动文字推送；不需要在 AstrBot 再配置一套 AI 模型。
- 图片、语音和文件暂不跨桥接上传；请使用灵犀网页或原飞书等渠道，插件会给出提示。
- 主动发送要求平台适配器支持主动消息。QQ 官方适配器依赖缓存的回复消息 ID，无法可靠确认主动送达，本插件暂时禁用其主动推送；普通私聊问答仍可使用。
- 同一个平台机器人只保留一个接收入口，避免灵犀原生连接和 AstrBot 同时处理同一消息。更换接入方式前先完成平台配置和测试。

## 接口与兼容性

本插件按 AstrBot `v4.29.1` 官方源码核对，声明兼容 `>=4.29.1,<5`；已完成离线模拟测试，真实平台仍需绑定后联调。

| 灵犀接口（相对于 `/api/v1`） | 用途 |
|---|---|
| `GET/PUT/DELETE /integrations/astrbot/binding` | 查询、绑定、解绑私聊 |
| `POST /chat` | 使用绑定的 conversation_id 运行灵犀 AI |
| `POST /integrations/astrbot/outbox/claim` | 领取待发送消息和 300 秒租约 |
| `POST /integrations/astrbot/outbox/{id}/ack` | 携带 lease_token，确认 sent 或 failed |

API 请求使用账号 Token、禁止跟随 HTTP 重定向，凭据不进入消息或日志。聊天的 `request_id` 仅用于关联，服务端不把它作为幂等键。

开发验证：

```sh
python -m unittest scripts.tests.test_astrbot_plugin -v
docker compose -f docker-compose.astrbot.yml --profile astrbot config --quiet
```

参考：[AstrBot 官方仓库](https://github.com/AstrBotDevs/AstrBot/tree/v4.29.1)、[事件 API](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/platform/astr_message_event.py)、[主动发送 API](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/astrbot/core/star/context.py)、[插件配置](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.1/docs/en/dev/star/guides/plugin-config.md)。这里只调用公开接口，不复制 AstrBot 的实现。
