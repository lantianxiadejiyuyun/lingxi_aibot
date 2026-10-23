# 飞牛 NAS 部署

一个 Compose 项目管理灵犀、NapCat、Qdrant 与可选 Ollama；飞书官方 SDK 已合并到灵犀镜像，由 Rust 随账号设置自动管理。生产环境仅需 Docker 和 Bash。此指南使用 `/vol1/docker/lingxi-v2` 与端口 `18081`；按实际存储卷及局域网 IP 调整。

## 构建镜像

在源码根目录构建 NAS 对应架构的镜像。例如 x86-64 飞牛：

```sh
docker build --platform linux/amd64 -t lingxi:2.0.0 .
docker pull --platform linux/amd64 qdrant/qdrant:v1.19.0
docker save -o lingxi-fnos-images.tar lingxi:2.0.0 qdrant/qdrant:v1.19.0
```

将镜像包、`docker-compose.fnos.yml`、`scripts/deploy.sh` 和完整的 `integrations/napcat-webui-handoff/` 目录传到 NAS 部署目录，保持 `scripts/`、`integrations/napcat-webui-handoff/` 的相对位置，将 Compose 文件保存为 `compose.yml`。一键登录页面会只读挂载进 NapCat；缺少该目录时容器会拒绝启动，不会创建空目录掩盖文件缺失。已有其他构建标签时可另行设置 `.env` 中的 `LINGXI_IMAGE`。

NapCat 固定为 `v4.18.37` 及镜像 digest，首次启动会拉取该镜像。离线 NAS 可在有网络的机器拉取并导出，再导入本地固定标签，并在 `.env` 的 `NAPCAT_IMAGE` 指定这个已验证的标签。

## 首次安装

在新建的空部署目录中执行：

```sh
cd /vol1/docker/lingxi-v2
sudo docker load -i lingxi-fnos-images.tar
sudo bash scripts/deploy.sh init --fnos --host 192.168.1.10 --username admin
sudo bash scripts/deploy.sh up
sudo bash scripts/deploy.sh status
```

`init` 先提示输入至少 12 字符的管理员密码，再创建权限为 `600` 的 `.env`，生成 Qdrant 和 OneBot 独立随机密钥、数据目录与 NapCat HTTP 初始配置。密码不会写入部署文件。已有 `.env` 时拒绝覆盖；已有账号或迁移数据不要运行首次初始化。

若首次初始化在配置生成后因构建或数据库初始化失败中断，修复原因后用 `sudo bash scripts/deploy.sh init-account --username admin` 继续创建首个账号。已有账号时后端会拒绝此命令，不会重置密码或数据。

`status` / `urls` 会直接显示灵犀和 NapCat 地址。默认分别为 `http://你的NAS局域网IP:18081/`、`http://你的NAS局域网IP:6099/webui/`。登录灵犀管理员账号后，可从 QQ 接入页面一键登录 NapCat，无需到启动日志查找登录密钥。直接访问 NapCat 地址时，NapCat 自己的密钥认证仍然生效。应用仅发布到指定 LAN IP 和回环地址，NapCat 只发布管理端口，OneBot 3000 和 Qdrant 6333 留在 Compose 内网。

Compose 将 NapCat 的 `config` 目录只读挂载到应用的 `/run/napcat-config`，通过 `LINGXI_NAPCAT_CONFIG_DIR` 读取当前 `webui.json`。管理员点击登录后，后台通过 `LINGXI_NAPCAT_WEBUI_INTERNAL_URL`（默认 `http://napcat:6099`）调用 NapCat 认证接口，浏览器只接收其标准会话凭据，并通过 NapCat 同源登录页面建立会话。长期登录密钥只在服务端使用，不进入浏览器或 URL；会话凭据也不放入 URL。NapCat 原有认证和会话有效期保持生效。

该入口仅向已登录的灵犀管理员提供，无需挂载 Docker socket 或 QQ 登录目录。新安装由初始化脚本设置 UID 1000 的配置目录归属；迁移时保留现有密钥和目录权限，应用须能读取此配置。登录页面目录只读挂载在 NapCat 的 `/app/napcat/static/lingxi-login`，不会覆盖 NapCat 自带页面。外部 NapCat（`LINGXI_NAPCAT_MANAGED=false`）仍使用普通管理地址。反向代理部署时，需完整转发 NapCat 的 `/webui/` 路径及其子目录，包括 `/webui/lingxi-login/`。

打开 NapCat 后仍需登录机器人 QQ。在灵犀 QQ 接入中采用部署提供的 OneBot 连接，填写主人和机器人 QQ，并按页面复制的回调地址/密钥设置 NapCat HTTP 客户端。初始化不会自动发送 QQ 消息或开启群回复。向量地址为 `http://qdrant:6333`，密钥与 `.env` 的 `LINGXI_QDRANT_API_KEY` 一致。该编排允许访问内网服务，适用于可信账号环境。

飞书只需在「设置 → 通知渠道」配置 App ID / Secret，再开启飞书私聊；无须额外桥接容器或手动复制桥接 Token。绑定流程见 [飞书私聊](飞书私聊.md)。

## 合并已有 NapCat / 飞书部署

先执行备份，保留原容器、配置和数据卷。在 `.env` 设置以下值，NapCat 数据路径指向原来的 `config`、`plugins`、`qq` 三个目录的共同父目录：

```dotenv
COMPOSE_PROFILES=napcat
LINGXI_FEISHU_EMBEDDED=true
LINGXI_PUBLIC_URL=http://192.168.1.10:18081
LINGXI_NAPCAT_MANAGED=true
LINGXI_NAPCAT_DATA_DIR=/vol1/docker/lingxi_napcat/data
LINGXI_NAPCAT_WEBUI_URL=http://192.168.1.10:6099/webui/
LINGXI_NAPCAT_HTTP_URL=http://napcat:3000
NAPCAT_BIND_IP=192.168.1.10
```

将原 OneBot HTTP 服务的 Token 安全填入 `LINGXI_NAPCAT_HTTP_TOKEN`，不要改动已有配置文件或 QQ 登录目录。先停止旧 NapCat，再由当前项目启动，避免端口和登录文件冲突；确认其它机器人客户端已切换到新服务地址，再移除旧网络连接。需要保留外部 NapCat 时，将 `COMPOSE_PROFILES` 留空、`LINGXI_NAPCAT_MANAGED=false`，按实际地址设置 WebUI 与 HTTP URL。

飞书应先确认独立桥接收件箱已排空，停止旧桥接容器，再从运行命令中移除 `docker-compose.feishu.yml` 并重建 app。这个旧覆盖文件会显式关闭内置 SDK，供需要独立部署的人继续使用。已有身份绑定和会话保留；不要清理原数据或旧收件箱卷。

## 从旧版迁移

先备份原 MySQL、配置与图片目录，停止旧应用写入，再按 [v1 数据导入](Rust部署与迁移.md#v1-数据导入) 导出并导入空的 v2 主库。不要先初始化管理员再导入。

原账号、对话、记忆和记录由 Rust 导入器迁移；旧配置与专用记录加密归档，模型和外部渠道需重新配置。迁移后核对可见记录、图片及数据库完整性，再切换 QQ/通知入口。两套数据库不会持续同步，不要让同一机器人同时接入两套服务。

## 运维与升级

```sh
cd /vol1/docker/lingxi-v2
sudo bash scripts/deploy.sh status
sudo bash scripts/deploy.sh backup
```

`backup` 短暂停止当前运行的服务，复制 app 完整 `/data`（含主库、`master.key`、QQ 库、资源、内置飞书收件箱）、Qdrant 存储和 NapCat 配置/登录目录，保存 `.env`、Compose 文件与已存在的 `integrations/napcat-webui-handoff/` 页面资源，随后恢复原先运行的容器。备份目录权限为 `700`；若中途失败，会留下 `INCOMPLETE` 标记。备份不会删除数据卷。可选 Ollama 模型可重新下载，不包含在此备份中。

同时保存权限为 `600` 的 `compose.resolved.yml`，将 `include`、`extends` 和覆盖文件合并为一份完整编排快照。该文件包含展开后的密钥和宿主机绝对路径，只能作为私密备份保存；恢复到另一目录或机器时先核对并调整挂载路径。不要将它提交到 Git 或公开分享。

```sh
sudo docker load -i new-image.tar
# 同步发行包中的 integrations/napcat-webui-handoff/，保留原目录结构。
# 修改 .env 中的 LINGXI_IMAGE 为已导入的新标签后启动。
sudo bash scripts/deploy.sh up
```

脚本每次创建独立备份目录，升级时保留上一镜像。回退时停止服务，先另存升级后数据，再将 `app-data` 恢复到 `data/`，NapCat 三个目录恢复到原位置，备份中的 `napcat-webui-handoff/` 恢复到部署目录的 `integrations/napcat-webui-handoff/`，Qdrant 存储恢复到原 `lingxi-v2_qq_vectors` 卷，并恢复对应配置和镜像。数据库与 `master.key` 必须成套恢复。不要运行 `docker compose down -v` 作为升级步骤。

已有 Ollama 等覆盖文件时，对 `up/status/backup` 使用一致的文件参数：

```sh
sudo bash scripts/deploy.sh status --file compose.yml --file docker-compose.ollama.yml
sudo bash scripts/deploy.sh backup --file compose.yml --file docker-compose.ollama.yml
sudo bash scripts/deploy.sh up --file compose.yml --file docker-compose.ollama.yml
```

源码中已移除旧 Python 后端。已有旧 NAS 部署与其数据库需在确认迁移完成后单独处理，Git 更新不会自动停用或删除它们。

## 可选：本地群聊接话判断

将源码中的 `docker-compose.ollama.yml` 复制到部署目录。它提供独立的 Ollama 服务，关闭云模型、限制为 2 个 CPU / 1400 MiB 内存 / 单个推理请求，不向宿主机发布端口。

```sh
sudo docker compose -f compose.yml -f docker-compose.ollama.yml up -d ollama
sudo docker compose -f compose.yml -f docker-compose.ollama.yml exec ollama ollama pull qwen3:0.6b
```

在「QQ 群 → 判断设置」填入 `http://ollama:11434` 和 `qwen3:0.6b`，先用测试消息查看耗时。然后在各群设置中选择「只观察」，确认群会话中的判断记录符合预期后再选「自动接话」。安装模型不会自动启用任何群。

明确以机器人称呼开头时，先用本地规则判断：叫名字接话、要求等待则等待、要求别回复则旁听；额度和间隔仍生效。其他语境交给小模型，只看当前群最近几条公开消息，不读取主人私聊、人设主库、跨群记录，也不执行工具。它输出 `reply / wait / ignore`；`wait` 表示等下一条消息再判断，不会定时补发。开启自动接话后，由该群原有的聊天模型生成最终回复；这部分仍可能使用你配置的云服务。

默认自动接话至少间隔 120 秒、每小时最多 6 次，每群每小时最多判断 60 次。被 @ 的回复沿用原配置；超时、模型繁忙、无效结果、旧消息均不会触发新的自动回复。判断结果与耗时保留 7 天，消息正文仍按原来的主人/群成员分库规则保存。

这是使用开源小模型实现的接话分类功能，并非 Jev 的复刻或性能对标。Qwen3 0.6B 的 Q4_K_M 下载约 523 MB，运行内存还包括上下文与服务本身。J4125 限制 2 核、2048 上下文的单句测试中，首次推理约 32 秒，热调用约 7–8 秒，运行时内存约 1.07 GiB；小模型也出现了称呼和等待意图误判。这是规则优先的原因，不代表复杂群聊判断已可靠。建议先观察；更强的同局域网电脑可以运行较大的 Ollama 模型，再修改地址。不要把普通分类结果当作可靠的权限判断，也不显示未经校准的“置信度”。

参考：[Ollama 结构化输出](https://docs.ollama.com/capabilities/structured-outputs)、[Qwen3 0.6B 模型](https://ollama.com/library/qwen3:0.6b)。
