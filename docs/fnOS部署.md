# 飞牛 NAS 部署

Rust 服务内嵌 Vue 前端，生产环境仅需 Docker。此指南使用独立目录 `/vol1/docker/lingxi-v2` 和端口 `18081`；请按实际存储卷及局域网 IP 调整，不要覆盖旧版的数据目录。

## 构建镜像

在源码根目录构建 NAS 对应架构的镜像。例如 x86-64 飞牛：

```sh
docker build --platform linux/amd64 -t lingxi:2.0.0-fnos-20261022-bili .
docker pull --platform linux/amd64 qdrant/qdrant:v1.19.0
docker save -o lingxi-fnos-images.tar lingxi:2.0.0-fnos-20261022-bili qdrant/qdrant:v1.19.0
```

将镜像包和 `docker-compose.fnos.yml` 传到 NAS 部署目录，把后者保存为 `compose.yml`。已有其他构建标签时，在 `.env` 设置 `LINGXI_IMAGE`，与实际导入的镜像一致。

## 首次安装

在 NAS 部署目录新建 `.env`，设置以下字段；Qdrant 密钥用随机字符串替换，文件权限设为 `600`。

```dotenv
LINGXI_NAS_IP=192.168.1.10
LINGXI_PORT=18081
LINGXI_IMAGE=lingxi:2.0.0-fnos-20261022-bili
LINGXI_QDRANT_API_KEY=替换为随机长密钥
```

服务以 UID/GID `1000:1000` 运行。创建专用空目录后设置权限：

```sh
cd /vol1/docker/lingxi-v2
mkdir -p data backups
chmod 700 backups
chmod 600 .env
sudo chown 1000:1000 data
sudo docker load -i lingxi-fnos-images.tar
```

全新安装需初始化管理员；已有迁移数据时跳过此步骤。密码至少 12 个字符，临时从终端传入，不写入 Compose 或镜像：

```sh
read -r -s -p '管理员密码：' LINGXI_ADMIN_PASSWORD
export LINGXI_ADMIN_PASSWORD
sudo --preserve-env=LINGXI_ADMIN_PASSWORD docker compose run --rm -e LINGXI_ADMIN_PASSWORD app init --username admin
unset LINGXI_ADMIN_PASSWORD
sudo docker compose up -d --wait
```

打开 `http://你的NAS局域网IP:18081/`。应用发布到指定的 LAN IP 和本机回环；Qdrant 不发布宿主机端口。QQ 接入设置中的向量地址为 `http://qdrant:6333`，密钥与 `.env` 一致。该编排允许访问 NAS 内的 NapCat、模型和向量服务，仅用于可信用户环境。

## 从旧版迁移

先备份原 MySQL、配置与图片目录，停止旧应用写入，再按 [v1 数据导入](Rust部署与迁移.md#v1-数据导入) 导出并导入空的 v2 主库。不要先初始化管理员再导入。

原账号、对话、记忆和记录由 Rust 导入器迁移；旧配置与专用记录加密归档，模型和外部渠道需重新配置。迁移后核对可见记录、图片及数据库完整性，再切换 QQ/通知入口。两套数据库不会持续同步，不要让同一机器人同时接入两套服务。

## 运维与升级

```sh
cd /vol1/docker/lingxi-v2
sudo docker compose ps
sudo docker compose logs --tail=100 app
```

升级前停止应用写入，备份 `data/`、`.env`、`compose.yml`，并另行备份 Qdrant 的 `lingxi-v2_qq_vectors` 卷或集合快照。数据库与 `master.key` 必须成套恢复。

```sh
sudo docker compose stop app
sudo tar -czf backups/v2-before-upgrade.tgz data .env compose.yml
sudo chmod 600 backups/v2-before-upgrade.tgz
sudo docker load -i new-image.tar
# 修改 .env 中的 LINGXI_IMAGE 为已导入的新标签后启动。
sudo docker compose up -d --wait app
```

每次升级使用不同的备份文件名并保留上一镜像。回退时恢复对应的 Compose、镜像和数据快照，先另存升级后产生的数据。不要运行 `docker compose down -v` 作为升级步骤。

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
