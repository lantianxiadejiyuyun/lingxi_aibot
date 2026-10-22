# 个人 QQ 直连灵犀

接法是 **个人 QQ ↔ NapCat ↔ 灵犀**，无需 AstrBot。NapCat 登录助手 QQ；灵犀继续使用现有的模型、人设、记忆、日程和待办。

## 使用

1. 管理员在 NapCat 管理页扫码登录一个作为助手的 QQ。
2. 用自己的 QQ 添加助手 QQ 为好友。在灵犀「设置 → QQ 个人号」生成绑定码。
3. 在与助手 QQ 的私聊中发送页面上的完整 `/lingxi 绑定 ...` 命令。绑定码 10 分钟有效、只能使用一次；生成新码会撤销旧码。
4. 收到绑定成功回复后，直接聊天即可。`/help` 查看模型切换、上下文压缩等现有命令。

绑定连接的是**当前灵犀账号的数据**，每个 QQ 私聊只能绑定一个灵犀账号。绑定码不要发到群里；服务器共享 Token 不需要发给 QQ。

当前接收私聊文字，群聊和助手自身发出的消息不会触发 AI。图片、语音输入请使用灵犀网页或飞书。通知渠道可发送文本与图片；只在接口确认成功后记录送达。

## 提醒和主动消息

- 日程、待办提醒和简报：在「设置 → 通知渠道」选择 **QQ 个人号（NapCat）**。
- 主动聊天：在「主动陪伴」单独开启，并选择这个渠道。作息、免打扰、间隔和每日额度继续生效。
- 绑定本身不会开启主动消息，也不会改变其他渠道设置。
- `/lingxi 身份` 查看 QQ 身份；`/lingxi 状态` 查看绑定；`/lingxi 解绑` 或网页「解除绑定」断开关联。

## Docker 部署

先按[部署说明](部署.md)启动灵犀，再增加 NapCat。已有灵犀升级后执行 `python -m flask init-db`，创建 QQ 绑定、短期绑定码和消息去重表。

1. 在项目 Python 环境运行初始化脚本（依赖项目已有的 `python-dotenv`）：

   ```bash
   python scripts/configure_napcat.py \
     --env-file data/.env \
     --data-dir data/napcat \
     --ws-url ws://app:8000/lingxi/api/v1/integrations/onebot/ws
   ```

   `lingxi` 替换为实际后台入口；同一 Docker 网络内使用服务名 `app`，不要写 `127.0.0.1`。脚本写入 `ONEBOT_ENABLED`、随机共享 Token 与 `ONEBOT_API_URL=http://napcat:3000`，并生成 NapCat 配置。已有 NapCat 配置时脚本会停止，避免覆盖账号设置。

2. 在根 `.env` 设置 `NAPCAT_NETWORK` 为灵犀所在的已有 Docker 网络（默认 `lingxi-aibot_aibot`）。需要局域网管理时再设置 `NAPCAT_BIND_IP` 为服务器局域网 IP。

3. 启动并重启灵犀以加载配置：

   ```bash
   docker compose -p lingxi-napcat -f docker-compose.napcat.yml up -d
   docker compose up -d --force-recreate app
   ```

   有独立媒体 worker 时也重建该服务，使下载完成通知使用新渠道。

4. 打开服务器的 `6099/webui`。登录令牌保存在 `data/napcat/config/webui.json` 的 `token` 字段。扫码登录助手 QQ 后，回灵犀完成绑定。

管理页默认只绑定 `127.0.0.1:6099`；业务 HTTP `3000` 不映射到宿主机，反向 WebSocket 共用灵犀端口。无需开放公网端口。NapCat 数据保存在 `data/napcat/qq`，配置保存在 `data/napcat/config`，都应保留。

可在运行配置 `data/.env` 设置 `ONEBOT_WEBUI_URL=http://服务器局域网IP:6099/webui`，管理员的 QQ 页面会出现入口；该地址不携带登录令牌。

## 排查

**等待 QQ 登录**：先查看 NapCat 是否在线，再核对反向连接地址、后台入口和两端共享 Token。入站连接使用 `Authorization: Bearer ...`；不接受 URL 中的 Token。NapCat 默认的 `onebot11.json` 会用于新账号，已有账号还需核对 `onebot11_QQ号.json`。

**未回复**：从自己的 QQ 私聊助手，先发 `/lingxi 身份`；确保消息不是群聊、助手自己发送的消息或只有图片。随后检查绑定与聊天模型配置。

**通知失败或结果未知**：查看灵犀通知记录与 NapCat 在线状态。超时可能意味着已发送但确认未返回，因此不会盲目重发。多段消息会标明部分送达；不要直接重复发送整段内容。

**断线后重复上报**：同一机器人、同一消息 ID 会去重，避免再次调用模型和执行任务。去重保留期有限；这不是无限期的 exactly-once 承诺。服务在执行中重启时，也不会自动重放可能已执行的工具。

当前部署沿用项目的单 Gunicorn worker、多线程模式。反向连接的队列有上限；需要横向扩容时应先将接收队列与会话串行处理移至独立 worker。

## 参考

- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)
- [NapCat Docker](https://github.com/NapNeko/NapCat-Docker)
- [OneBot v11 反向 WebSocket](https://github.com/botuniverse/onebot-11/blob/master/communication/ws-reverse.md)
