# 灵犀 Lingxi 2.0

Rust 后端 + Vue 3 个人 AI 工作空间。整理对话、待办、日程、笔记与生活记录，并为 QQ 群成员建立独立的人物记忆。

## 当前架构

- **后端**：Rust / Axum / Tokio / SQLx，版本化 API、流式聊天、持久任务和定时计划。
- **前端**：Vue 3 / TypeScript / Vite，组件化界面、响应式导航、Markdown 回复和群人物管理。
- **主人主库**：SQLite WAL，保存个人资料、主人的性格偏好及对话。
- **群成员独立库**：独立 SQLite 保存原文与证据，专用 Qdrant 保存人物向量，按账户、群号、QQ 号隔离。
- **图片与表情**：模型实际看图后挑选喜欢的图片，或由主人发图 / 回复图片命令收藏；支持去重、标签、搜索和原图下载。
- **主动陪伴**：结合人设、主库记忆和近况，按个人时区与免打扰安排主动分享；支持站内、QQ 私聊、AstrBot、飞书和 Server酱。
- **B 站书房**：按兴趣发现视频，读取原始字幕并形成心得；支持扫码登录、定时阅读、字幕定位、阅读小记和主动陪伴引用。详见 [B 站书房](docs/B站书房.md)。
- **飞书私聊**：官方 Node SDK 已内置到应用镜像，随配置自动启动和停止；一次性身份绑定、持久收件箱与消息去重，主人的文字对话和记忆归入主库。详见 [飞书私聊](docs/飞书私聊.md)。
- **部署**：一个入口管理 Rust/Vue、内置飞书接入、NapCat 和专用向量库。QQ 页面直接打开 NapCat 管理地址；已有外部 NapCat 也可继续使用。

## 开始使用

Linux 上使用下面的部署入口，自动生成密钥和首次 NapCat 配置、初始化管理员。默认地址是 `http://127.0.0.1:8000`。Windows 可在 WSL 运行，或按 [v2 部署与迁移](docs/Rust部署与迁移.md) 使用 Docker Compose 命令。

飞牛部署说明见 [fnOS 部署指南](docs/fnOS部署.md)。配置模板为 `.env.docker.example`，不再使用 MySQL 或 Flask 配置。

```sh
bash scripts/deploy.sh init --username admin
bash scripts/deploy.sh up
bash scripts/deploy.sh status  # 显示服务状态、灵犀与 NapCat 地址
# 升级前完整备份（短暂停机，完成后恢复原先运行的服务）：
bash scripts/deploy.sh backup
```

登录后先在「设置 → 对话模型」连接自己的模型。在 QQ 页打开 NapCat 管理界面并登录机器人 QQ，再填主人 QQ、机器人 QQ 和回调配置。在「QQ 群与会话」中逐个添加群，每个群有固定的独立会话，可分别选择模型、人设、回复和人物记忆开关；接入设置管理共用的主人 QQ、机器人 QQ、NapCat / OneBot、嵌入模型及专用向量库。统一部署的 OneBot 地址可以在页面直接采用，无需记忆容器 IP。

界面采用粉色 `#EABCDD`、浅粉 `#F0DFEA`、暖白 `#F7F6F5`、淡紫 `#DDD1E9`、雾蓝 `#A4B5D5` 的统一主题。颜色集中在 `web/src/styles/tokens.css`，布局和组件样式分别维护。临时连接失败保留登录信息并提供重连入口；设置分页通过 URL 保留当前栏目。

图片收藏默认关闭，在「图片与表情 → 收藏偏好」开启并选择看图模型。主人可以附图或回复图片说「收藏这张图」，直接收藏，详见 [图片收藏说明](docs/Rust部署与迁移.md#qq-图片与表情收藏)。

主动陪伴默认关闭，在侧栏「主动陪伴」设置时段、频率、兴趣和接收渠道。保存后按计划检查，没有合适内容时保持安静。个人 QQ 与 AstrBot 的绑定也在此页面，飞书和 Server酱在「设置 → 通知渠道」配置，详见 [主动陪伴说明](docs/Rust部署与迁移.md#主动陪伴与私聊连接)。

## 记忆如何归属

| 身份 | 对话记录 | 性格与偏好 |
|---|---|---|
| 主人 | 主库中的专属会话 | 主库长期记忆 |
| 其他群成员 | 独立群成员 SQLite | 独立元数据 + 专用 Qdrant 向量库 |

身份只由配置的数字 QQ 号决定，不依赖昵称。性格观察保留原文依据、置信度和有效期，可查看、修正、删除。群聊回复不加载主人私人会话、私人记忆或个人工具。

## 开发与验证

```sh
npm ci --prefix web
npm run build --prefix web
npm test --prefix web
cargo test --workspace --locked
cargo run --bin lingxi -- serve
```

另开终端运行 `npm run dev --prefix web`，即可使用 Vite 开发服务。技术说明见 [Rust / Vue 架构](docs/Rust-Vue架构.md)。

## 迁移状态

v2 已重写核心记录、聊天、记忆、任务、主动陪伴与私聊接入，并新增 QQ 群人物分库。复杂多 Agent / 媒体工作流、重复日程实例展开和部分专用渠道协议尚未全部迁移，详见 [功能差异](docs/Rust部署与迁移.md#与-v1-的功能差异)。

当前分支已移除 Python / Flask 后端、重复部署源码和旧后端测试，只构建 Rust / Vue。Python 仅用于可选的 AstrBot 插件、旧数据库导出和源码打包，不参与灵犀服务运行。

旧实现可从 Git 提交 `2f8915f` 查看；历史说明与配置保存在 [v1 资料归档](docs/legacy/README.md)，不适用于当前版本。迁移仍使用 `scripts/export_legacy.py` 和 Rust 导入命令，回退需原部署及数据备份。

本项目使用 [PolyForm Shield License](LICENSE)。
