# 灵犀（Lingxi）— AI 个人工作生活管理基座

以 AI 为核心的个人助理基座：统一管理日程、任务、笔记、健身、出行、消费与定时提醒，并通过 AI 对话、
早晚午间简报、多渠道通知来帮你更好地安排工作与生活。

- **技术栈**：Python · Flask（后端+前端渲染）· Jinja2 + HTMX 风格原生 JS · MySQL · APScheduler
- **AI 能力**：每位用户自己的 API Key；支持 **OpenAI 兼容**（DeepSeek / OpenAI / 通义 / Kimi / Ollama）与 **Anthropic Messages**（Claude）；SSE 流式 + 工具调用
- **自进化**：AI 可自写 Python 技能（受限沙箱 + **子进程超时强杀** + 技能模板引导 + 人工启用）+ 定时自动梳理上下文（会话摘要 + 长期记忆）
- **RAG 语义检索**：笔记/网页/对话向量化（OpenAI 兼容嵌入接口），`semantic_search` 语义级检索，未配置时自动降级关键词搜索
- **联网搜索 + 调研工作流**：AI 对话直接「帮我搜一下/调研…」→ 联网搜索（Bing 免 key 国内可用，可换 SearXNG/Serper/Tavily）→ 自动生成**调研网页**（可点击 HTML 链接）→ 你说「收录进知识库」即提炼核心存入笔记（自动进 RAG 知识库）
- **智能日程**：AI 创建/修改日程自动检测时间冲突，冲突时主动提示并给备选
- **主动早安**：早报升级为对话式——早报会话内附带 AI 引导语，可继续追问
- **周报/月报**：定时汇总日程/任务/产出 → AI 生成 Markdown 报告 → 推送到渠道
- **语音助手**：浏览器语音输入（🎤）+ AI 回复朗读（🔊，可接 OpenAI 兼容 TTS）
- **生活三件套**：健身打卡（统计 + 每日提醒 + 每周 AI 分析）、出行计划（多段交通 + AI 生成行程 + 出发提醒）、消费记账（看板 + 可选 App REST API）
- **多用户**：数据按账号隔离；管理员可在设置页创建用户、绑定飞书身份；每用户一套内置定时任务
- **通知渠道**：可插拔架构，已实现 站内通知 / Server酱 / 飞书，后续可随时扩展；支持**自定义通知组**——把多个渠道打包成一个组（`group:组名`），默认渠道/各场景/AI 助手/定时任务选中即联动同步推送
- **定时任务**：内置 13 个任务（提醒扫描/到期扫描/三档简报/备份/上下文梳理/周报/月报/每周整理/健身提醒/健身周分析/出行提醒）+ 自定义定时提醒，均可在后台管理
- **后台界面**：侧栏按「工作 / 生活 / 智能 / 系统」分组，AI 助手置顶；顶栏随时可进对话；设置页 Tab 分组吸顶
- **部署**：宝塔 / 普通 Linux / Docker，见 **[docs/部署.md](docs/部署.md)**（含 MySQL 连库、网页端口、飞书、安全清单）

## 许可

本项目采用 [PolyForm Shield License 1.0.0](LICENSE)（[原文](https://polyformproject.org/licenses/shield/1.0.0)）。**以下为便于阅读的说明，法律效力以 `LICENSE` 全文为准。**

**可以**

- 个人学习、自用、修改
- 公司 / 团队内部部署给自己用
- 再分发源码或改过的版本（必须带上本许可和 `NOTICE`）

**不可以**

- 把灵犀（或其功能上的实质替代品）当作产品对外售卖
- 做付费托管 / SaaS / 代运营
- 提供与作者用本软件提供的产品相竞争的服务（免费当竞品也不行）

想对外运营或做商业产品，请联系仓库维护者单独授权。版权与业务范围见 [NOTICE](NOTICE)。

## 功能一览

| 模块 | 说明 |
|---|---|
| 📊 仪表盘 | 今日日程、待办、到期、完成数；快捷入口覆盖日历/任务/笔记/健身/出行/消费/网页/AI |
| ⚡ 安装引导 | 首次安装引导页：仅在系统未初始化（数据库未连接/未建表/无管理员）时显示，含完整安装命令；初始化后访问自动跳转登录/仪表盘 |
| 📅 日历 | 月视图 + 当日明细，重复事件（每天/周/月/年），提前提醒 |
| ✅ 任务 | 优先级、截止时间、项目、标签，筛选搜索 |
| 📝 笔记 | 快速记录，全文检索，作为 AI 的长期记忆 |
| 🤖 AI 助手 | 流式对话，**63 个工具**（日历/任务/笔记/网页/图片/技能/记忆/定时任务/通知/搜索/健身/出行/消费）；支持语音输入与朗读、语义检索、联网搜索与调研、一键清空所有会话 |
| 🌐 网页生成器 | AI 生成自由 HTML 网页；公开页推荐只填 HTTP 端口（如 `http://IP:8080/slug`），不必配证书/域名 |
| 🖼️ 图片生成 | OpenAI 兼容图片接口（硅基流动/FLUX/中转站等），对话生成 + AI 改图（编辑接口/降级重生成），图片库管理，可公开可私有 |
| 🧩 技能（自进化） | AI 自写 Python 工具函数（受限沙箱 + 子进程超时强杀 + 编写模板引导），新建默认禁用、人工启用后即可被对话调用，可在线启停/删除 |
| 🧠 记忆（自进化） | 定时自动梳理上下文：长对话压缩为摘要 + 跨对话抽取长期记忆（带重要度与过期时间，自动遗忘），注入每次对话；可手动立即梳理 |
| 💪 健身 | 训练记录（类型/时长/强度/卡路里/体重）+ 统计看板；每日 20:00 提醒、每周日 AI 周分析 |
| 🧳 出行 | 出行计划（多段交通、备选方案、预算）；一键 AI 生成逐日行程；出发前一天自动提醒 |
| 💰 消费 | 记账 + 按月筛选 + 分类/支付方式统计；可用 `API_TOKEN` 对接 App（`/api/v1/expenses`） |
| 🧹 每周整理 | 自动归档超期任务、合并重复笔记、网页坏链体检、清理旧通知，周日自动执行并推送报告 |
| ⏰ 定时任务 | 内置 13 个任务 + 自定义定时提醒（cron），可改时间/开关/手动执行/**推送渠道** |
| 🗄️ 备份与数据 | 自动备份（流式 + gzip 压缩，大数据量内存恒定）、手动备份、一键导出下载、一键重置业务数据 |
| 🔔 通知中心 | 站内通知聚合，Server酱/飞书推送；**各场景推送渠道统一配置**（简报/周报/备份/整理/提醒，任务页可单独覆盖），失败原因可见 |
| 🤖 飞书机器人 | **双向**：收到飞书消息自动 AI 回复（可调工具），也可 API 主动发送到指定群/人 |
| ⚙️ 设置 | Tab 按 **账户 / AI / 通知 / 生成 / 系统** 分组：账号与用户、模型与人设、渠道/飞书/简报、图片/视觉、备份/域名 |

## 快速开始（本地开发）

```powershell
# 1. 创建虚拟环境并安装依赖（Python 3.11+）
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# 2. 配置 .env（复制 .env.example 修改：MySQL 连接、DeepSeek API Key、渠道密钥）
copy .env.example .env

# 3. 初始化数据库（建表 + 管理员 + 内置定时任务）
$env:FLASK_APP = "run:app"
.\.venv\Scripts\python -m flask init-db

# 4. 启动（默认 http://127.0.0.1:5000）
.\.venv\Scripts\python run.py
```

默认管理员：`admin / admin123`（**登录后请立即在「设置 → 账号」修改密码**）。

## 配置说明（.env）

| 变量 | 说明 |
|---|---|
| `MYSQL_HOST / PORT / USER / PASSWORD / DB` | MySQL 连接。同机用 `127.0.0.1`；Docker 接宿主机用 `host.docker.internal`；compose 自带库用 `db`。详见 [部署教程](docs/部署.md) |
| `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL` / `LLM_PROTOCOL` | 仅作安装时首个管理员的初始值。**运行时每位用户必须在「设置 → 模型与人设」填写自己的 Key**，互不共用。协议：`openai`（chat.completions，默认 DeepSeek）或 `anthropic`（Claude `/v1/messages`） |
| `SC_KEY` | Server酱 SendKey（https://sct.ftqq.com/ 获取，留空则渠道不可用） |
| `FEISHU_WEBHOOK_URL / FEISHU_SECRET` | 飞书群机器人 Webhook（可选加签，留空则渠道不可用） |
| `DEFAULT_CHANNELS` | 默认推送渠道，逗号分隔：`inapp,serverchan,feishu` |
| `PAGE_PORT` / `PAGE_HOST` | 公开网页独立 HTTP 端口（推荐，无需证书）；主机留空则自动用本机 IPv4 |
| `ADMIN_DOMAIN` / `PAGE_DOMAIN` | 可选：已有 Nginx/Caddy HTTPS 时的后台域名 + 网页域名（也可在设置页修改） |
| `IMAGE_BASE_URL` / `IMAGE_API_KEY` / `IMAGE_MODEL` / `IMAGE_SIZE` | 图片生成服务（OpenAI 兼容接口，也可在设置页修改） |
| `VISION_BASE_URL` / `VISION_API_KEY` / `VISION_MODEL` | 视觉（多模态识图）模型，独立于沟通模型与图片模型（OpenAI 兼容 chat/completions + 图片输入，也可在设置页修改；未配置时飞书收图仅保存不识别） |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` | RAG 语义检索嵌入服务（未配置时自动降级关键词搜索） |
| `SEARCH_PROVIDER` / `SEARXNG_BASE_URL` / `SERPER_API_KEY` / `TAVILY_API_KEY` | 联网搜索提供方（默认 bing 免 key，也可在设置页修改） |
| `API_TOKEN` | 可选：全局回退 Token（记到第一个管理员）。**推荐在「设置 → 账号」为每个用户生成独立 Token** |
| `ADMIN_ENTRY` | 可选：后台安全入口路径。设置后须访问 `https://域名/<入口>/…`，不带入口访问后台 404 |
| `SECRET_KEY` | 会话密钥，**公网部署必须改成随机长字符串** |
| `SESSION_COOKIE_SECURE` | 公网 HTTPS 部署设为 `1` |
| `SCHEDULER_ENABLED` | 定时任务总开关（默认 1） |

> 渠道参数（Server酱/飞书/默认渠道/简报时间）也可以在网页「设置」里修改，页面值优先于 .env。

## 飞书机器人（双向：接收消息 + AI 回复）

除了群机器人 Webhook（单向推送），还支持**飞书自建应用机器人**（可接收用户消息并由 AI 回复）：

1. 飞书开放平台创建**自建应用** → 应用能力开启「机器人」→ 权限添加 `im:message` 相关权限并发布版本
2. 事件订阅 → 添加事件 `im.message.receive_v1`，回调地址填 `https://你的域名/feishu/event`（**必须公网 HTTPS 可达**，国内服务器需 ICP 备案域名），可填验证令牌/加密密钥
3. 在网页「设置 → 飞书」填入 App ID / App Secret / 验证令牌 / 加密密钥 / 默认发送目标

工作方式：用户在飞书里给机器人发消息 → 灵犀 自动创建/续接会话 → AI（可调用全部 63 个工具）生成回复 → 通过 API 发回该会话。多轮对话上下文自动保留（chat_id 映射）。主动推送（通知/简报）可走该渠道发到指定群或人。

**收消息有两种接入方式**（「设置 → 飞书」切换）：

| 方式 | 说明 | 适用 |
|---|---|---|
| HTTP 回调 | 开放平台 POST 到 `https://你的域名/feishu/event`，需公网 HTTPS | 已备案的云服务器 |
| 官方 SDK 长连接 | 进程用 `lark-oapi` 主动连飞书，无需公网回调；开放平台事件订阅选「使用长连接接收事件」。**构建的网页仍需公网 IPv4 才能在飞书里打开** | 本地开发 / 内网收消息；没有公网 IPv4 时网页链接无法展示 |

依赖：`pip install lark-oapi`（已写入 `requirements.txt`）。两种方式共用同一套 AI 回复逻辑，不要同时开。

## AI 工具调用（基座核心）

对话中 AI 会根据需要自动调用工具，例如：

- “明天下午 3 点提醒我开会” → `create_event`（含提醒）
- “我这周有哪些任务没做完？” → `list_tasks`
- “帮我记一下：服务器 root 密码已改” → `create_note`
- “每天 9 点半提醒我喝水” → `create_reminder_job`
- “把明天的日程发到飞书” → `list_events` + `send_notification`
- “帮我做一个个人主页，放上我的项目链接” → `create_page`（生成完整 HTML 网页）
- “帮我画一张星空下的柴犬” → `generate_image`（对话里直接显示图片）
- “把这张图的背景改成黄昏” → `edit_image`（图像编辑或降级重生成）
- “帮我创建一个新技能：把文字转成首字母大写” → `create_skill`（AI 写代码，人工启用后生效）
- “帮我记住：我下周三要出差” → `remember`（写入长期记忆，注入后续对话）
- “今天跑步 40 分钟” → `create_fitness_record`
- “帮我规划下周五去杭州，高铁往返” → `create_trip_plan` + `generate_trip_itinerary`
- “午饭 38 块微信支付” → `create_expense`

对话人设可在「设置 → 模型与人设 → 对话人设」改名字、性格预设（默认/专业/温柔/幽默/老师/教练/自定义）、称呼、回复详细度和补充说明。工具规则不会被覆盖，下一轮对话生效。

工具通过 `@register_tool` 注册（`app/ai/tools/` 下自动发现），**新增一个技能 = 新增一个函数**。
定时任务的执行动作通过 `@register_action` 注册（`app/scheduler.py`），同样即插即用。

## 网页生成器

AI 对话中说“帮我做一个 … 网页”即可生成**完整自由 HTML 页面**（CSS/JS 内联），也可以手动编写：

- **管理页** `网页`：列表、在线编辑（左侧源码 + 右侧实时预览）、复制、删除、显示/隐藏开关
- **公开访问（推荐只填端口）**：「设置 → 网页站点」填端口（如 `8080`），保存后 `http://局域网IP:8080/<slug>` 即可打开，**不必配 HTTPS 或域名**。链接主机可留空（自动用本机 IPv4）。
- **AI 工具**：`create_page / get_page / list_pages / update_page / duplicate_page / delete_page`
- 后台可随时隐藏页面（前台 404）；私有页仍走后台登录后的 `/p/<slug>`
- 域名/证书仅为高级选项（已有 Nginx/Caddy 时再用）

## 图片生成（OpenAI 兼容接口）

AI 对话中直接说“帮我画一张 …”，或在「🖼️ 图片」页面手动生成：

- **服务商**：任何 OpenAI 兼容 images 接口（硅基流动 `https://api.siliconflow.cn/v1` 的
  FLUX/Kolors、各类中转站、OpenAI 等），在「设置 → 图片」填入 BaseURL / API Key / 模型，保存即生效
- **AI 改图**：对话说“把这张图的背景改成黄昏”→ `edit_image`：
  优先调用 `/images/edits` 图像编辑接口（OpenAI gpt-image 系列支持）；端点不支持时
  **自动降级**为“修改要求 + 原图提示词”重新生成新图（新图 `parent_id` 关联原图）
- **图片库**：网格浏览、公开/私有开关、下载、删除、改图、重新生成；
  默认仅登录可见，公开图片可嵌入到生成的网页中
- **AI 工具**：`generate_image / list_images / get_image / edit_image / set_image_public / delete_image`
- 生成结果落盘 `data/images/`（文件名不可枚举），对话里通过 Markdown 图片链接直接显示

## 自进化能力

### 🧩 技能（AI 自写工具）

AI 对话中说“帮我创建一个新技能：…” → AI 先用 `get_skill_template` 获取编写规范 → 用 `create_skill` 编写 Python 工具函数并存入技能库：

- **受限沙箱 + 子进程执行**：技能在**独立子进程**中执行（`app/services/skill_worker.py`），**10 秒超时强制终止**——死循环不再挂死主进程；静态扫描拦截 `import`、反射逃逸等特征为第一道防线
- **编写模板**：`get_skill_template` 返回参数签名推导规则、返回值约定、可用环境与完整示例，AI 写码更稳
- **人工审核启用**：新建默认禁用，到「🧩 技能」页查看代码、审核后手动启用，启用后即可在对话中被调用
- **在线管理**：启用/停用（停用即从工具注册表注销）、查看代码、删除；应用重启后自动加载已启用技能
- **AI 工具**：`get_skill_template / create_skill / list_skills / get_skill / update_skill / set_skill_enabled / delete_skill`

> ⚠️ 沙箱是「尽力而为的护栏」而非强安全边界：AI 生成的代码本质是服务器代码，启用前务必人工审查。

### 🔍 RAG 语义检索

- 笔记/网页/会话内容向量化（OpenAI 兼容 embeddings 接口，如硅基流动 `BAAI/bge-m3`），存 `embeddings` 表
- AI 工具 `semantic_search(query, sources, k)`：余弦相似度检索，回答“我什么时候提过…”“我写过关于…的内容吗”
- 笔记/网页增改删自动维护索引；上下文梳理时把会话摘要入索引；`flask reindex` 一键重建
- 未配置嵌入服务时自动降级为关键词搜索并提示

### 🌐 联网搜索 + 调研工作流

AI 对话里直接说「帮我搜一下 / 调研一下…」即可联网调查：

- **搜索提供方**（设置 → 联网搜索，保存即生效；在「AI」分组下）：
  - `Bing`（默认，免 key，**国内网络可用**）
  - `DuckDuckGo`（免 key，部分网络不可达）/ `SearXNG`（自托管）/ `Serper` / `Tavily`（需 Key）
- **调研工作流**（系统提示词内置规则）：搜索后 AI 自动用 `create_page` 生成**调研网页**
  （标题「📚 调研：主题」，结构化 HTML，含结论与可点击链接）→ 用网页地址回答你；
  你说「收录进知识库」时，AI 再用 `create_note` **提炼核心**保存为笔记（标签「调研」），
  笔记自动进入 RAG 语义检索知识库，以后随时能问到
- **AI 工具**：`web_search`（搜索）+ `fetch_page`（抓取网页正文供阅读）；聊天里 Markdown 链接可直接点击
- 测试：设置页「测试搜索」一键验证当前提供方

### 🧠 记忆（定时自动梳理上下文）

- **会话摘要**：长对话（>12 条）自动压缩为摘要（保留最近 8 条原文），注入后续对话 —— 上下文不再随对话变长而爆 token
- **长期记忆**：跨对话自动抽取稳定事实/偏好存「🧠 记忆」，注入每次对话；每条带**重要度（★1-5）与过期时间**，过期的自动遗忘（不再注入 + 梳理时清理）；AI 也可直接 `remember` 手动记录（可指定重要度/过期）
- **触发**：内置定时任务「上下文梳理」（默认每日 04:00，可在「定时任务」页改时间/开关）+ 「🧠 记忆」页手动「立即梳理」
- **AI 工具**：`remember / list_memories / delete_memory`

## 健身 / 出行 / 消费

页面、定时任务、**AI 对话**都能用。可在「定时任务」页改 cron / 开关 / 推送渠道。

### 💪 健身（`/fitness/`）

记录一次训练：类型（跑步/力量/游泳/骑行/瑜伽/球类/HIIT/其他）、时长、强度、卡路里、体重。顶部看板汇总次数、总时长、总消耗、最近体重。对话里说「今天跑步 40 分钟」即可入账。

内置任务：

- **健身提醒** 每天 20:00
- **健身周分析** 每周日 19:00，用近 7 天记录让 AI 写分析（没配 AI 则发统计）

**AI 工具**：`list_fitness_records` / `create_fitness_record` / `update_fitness_record` / `delete_fitness_record` / `analyze_fitness`

### 🧳 出行（`/travel/`）

新建计划：目的地、往返日期、交通多选、多段交通（出发/到达/历时/购票平台）、备选方案、预算、同行人。点「生成行程」或对话里说「帮我规划行程」会按天数写出逐日 Markdown 并保存在计划里。

内置任务 **出行提醒**：每天 09:00 扫描「明天出发」的计划，有则推送。

**AI 工具**：`list_trip_plans` / `get_trip_plan` / `create_trip_plan` / `update_trip_plan` / `generate_trip_itinerary` / `delete_trip_plan`

### 💰 消费（`/expenses/`）

记一笔：金额、分类（餐饮/交通/购物/娱乐/居住/医疗/教育/旅行/其他）、日期、支付方式（微信/支付宝/现金/银行卡/其他）。可按月份筛选，看板给出总支出、笔数、分类占比、按月汇总。对话里说「午饭 38 微信支付」即可记账。

**AI 工具**：`list_expenses` / `get_expense_stats` / `create_expense` / `update_expense` / `delete_expense`

#### App REST（可选）

给手机记账 App 用。**推荐**到「设置 → 账号」生成你自己的 Token（数据记在你的账号下）。也可在 `.env` 设全局 `API_TOKEN`（回退到第一个管理员，兼容旧配置）。请求头：

```
X-API-Token: <你的 Token>
# 或 Authorization: Bearer <你的 Token>
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/expenses` | 列表（`start_date` / `end_date` / `category` / `limit`） |
| GET | `/api/v1/expenses/stats` | 统计 |
| GET | `/api/v1/expenses/<id>` | 详情 |
| POST | `/api/v1/expenses` | 创建 `{amount, category, date, payment_method, notes}` |
| PUT/PATCH | `/api/v1/expenses/<id>` | 更新 |
| DELETE | `/api/v1/expenses/<id>` | 删除 |

未配置任何 Token 时上述接口一律 401。用户 Token 之间数据互相隔离。

## 多用户

管理员登录后，「设置 → 用户」可创建账号（自动补齐该用户的内置定时任务），并把飞书 `open_id`（`ou_…`）绑到某个账号上。飞书里未绑定的发送者会收到提示。各用户的日程/任务/笔记/对话/健身/出行/消费互相隔离。

单域名想隐藏后台时，在 `.env` 设 `ADMIN_ENTRY=一段随机路径` 后重启，后台变成 `https://你的域名/<入口>/login`。

## 通知渠道扩展

新增渠道只需两步（参考 `app/services/channels/feishu.py`）：

```python
from app.services.notify_service import BaseChannel, register_channel

@register_channel
class MyChannel(BaseChannel):
    name = "mychannel"          # 唯一标识
    display_name = "我的渠道"    # 中文名
    @property
    def configured(self) -> bool:  # 是否已配置（读取 settings/.env）
        return bool(get_setting_from("my_channel_url", "MY_CHANNEL_URL", ""))
    def send(self, title, body):   # 发送失败抛异常，异常信息会记录到通知中心
        requests.post(..., timeout=10)
```

模块放进 `app/services/channels/` 即被自动发现，设置页和 AI 工具自动可见。

## 部署到云服务器

完整步骤（**宝塔面板 / 普通 Linux / Docker / MySQL 建库连库 / 网页端口 / 飞书 / 安全清单**）：

**[docs/部署.md](docs/部署.md)**

摘要：

- 生产入口是 `wsgi.py` + **waitress 单进程多线程**（Docker 镜像为 gunicorn `-w 1 --threads 8`）。不要用 uvicorn，不要多 worker。
- MySQL 需预先建好 `utf8mb4` 库；连不上会进入 `/setup` 向导（令牌在 `data/setup_token.txt`）。
- 公开网页推荐只填 `PAGE_PORT`（纯 HTTP），不必配证书和域名。
- Docker 接宿主机 MySQL 时 `MYSQL_HOST=host.docker.internal`；自带库：`MYSQL_HOST=db` 且 `docker compose --profile mysql up -d --build app db`。
- 备份：每日自动导出到 `./data/backups/`（gzip）；也可 `python -m flask backup-now`。

## 后台界面

登录后左侧导航分组如下（窄屏自动收成图标）：

| 分组 | 入口 |
|---|---|
| （置顶） | 仪表盘、**AI 助手** |
| 工作 | 日历、任务、笔记、网页、图片 |
| 生活 | 健身、出行、消费 |
| 智能 | 技能、记忆 |
| 系统 | 定时任务、通知、设置（网页站点填端口即可，不必配 HTTPS） |

顶栏右侧有「AI 助手」按钮（对话页除外）。设置页 Tab 分成五组并吸顶：**账户**（账号 / 用户）、**AI**（模型与人设 / 语音 / 联网搜索）、**通知**（渠道 / 飞书 / 简报）、**生成**（图片 / 视觉）、**系统**（备份 / 网页站点）。公开网页推荐只填一个 HTTP 端口（如 8080），不必配证书和域名。

## 项目结构

```
app/
├── ai/            # AI 层：llm / registry(工具注册表) / executor(对话循环) / prompts / memory / briefing / tools/
├── blueprints/    # 路由：auth / dashboard / calendar / tasks / notes / pages / images / skills / memory / fitness / travel / expenses / expenses_api / setup / chat / jobs / notifications / settings_page / feishu
├── models/        # MySQL 模型（17 张表，时间统一 naive UTC 存储，个人数据按 user_id 隔离）
├── services/      # 业务逻辑 + 网页/图片/技能/记忆/健身/出行/消费 + rag + notify + channels/(Server酱/飞书)
├── scheduler.py   # APScheduler 调度框架 + 动作注册表
├── templates/     # Jinja2 模板
└── static/        # 样式与前端工具 JS
scripts/smoke_test.py   # 端到端冒烟测试（28 项）
scripts/test_pages.py   # 网页生成器功能测试（34 项，含域名分离）
scripts/test_pages_ai_e2e.py   # AI 对话真实生成网页（消耗一次 LLM 调用）
scripts/test_images.py   # 图片生成功能测试（21 项，需配置图片服务）
scripts/test_images_ai_e2e.py   # AI 对话生成/改图（消耗 LLM 调用）
scripts/test_skills.py   # 技能沙箱/启用/执行测试（16 项）
scripts/test_sandbox.py  # 技能模板/子进程执行/死循环超时强杀测试（7 项）
scripts/test_rag.py      # RAG 语义检索测试（9 项，无需真实嵌入服务）
scripts/test_web_search.py      # 联网搜索各提供方解析/fetch_page（14 项，mock）
scripts/test_research_workflow.py  # 调研工作流端到端（真实搜索+LLM：搜索→网页→知识库）
scripts/test_memory.py   # 上下文梳理/长期记忆测试（13 项，消耗 LLM 调用）
scripts/test_life_modules.py  # 健身/出行/消费 AI 工具 + 重置表 + App Token 分用户
scripts/test_persona.py       # 对话人设：系统提示词注入 + HTTP 保存/恢复默认
scripts/test_llm_protocol.py  # OpenAI / Anthropic 协议转换 + 每用户 API Key 隔离
scripts/test_feishu_receive.py  # 飞书 HTTP 回调 vs 官方 SDK 长连接
scripts/test_setup.py    # 安装引导页测试（9 项）
scripts/mock_image_server.py   # 本地 mock 图片接口（无真实 Key 时验证全链路）
docker/                 # Caddy 配置
```

## 冒烟测试

```powershell
# 服务器运行中执行（覆盖登录/8 页面/CRUD/通知/定时任务/简报/调度器提醒/SSE 降级）
.\.venv\Scripts\python scripts\smoke_test.py
```

## 安全建议（公网）

- 登录接口已限流（10 次/分钟）+ CSRF 防护 + 密码哈希存储
- 联网抓取（fetch_page/坏链体检/图片下载）仅允许**公网地址**（SSRF 防护：拒绝回环/私网/链路本地/云元数据，重定向逐跳校验）
- 务必修改默认密码、`SECRET_KEY`、`SESSION_COOKIE_SECURE=1`
- 所有软删除数据可回滚；每日自动备份

## 文档

- [部署教程](docs/部署.md) — 宝塔 / 普通 Linux / Docker / MySQL / 网页端口 / 飞书 / 运维必读
- [发行目录](release/README.md) — `python scripts/pack_release.py` 生成可上传的 `release/lingxi/` 与 zip
- [项目文档](docs/PROJECT.md) — 架构、数据库（17 张表）、AI 层、健身/出行/消费、多用户、路由、安全、测试与开发约定
- [使用说明](docs/使用说明.html) — 面向使用者的功能说明（浏览器打开即可）
- [详细规划方案](docs/PLAN.md) — 原始规划（后续增量以 PROJECT.md 为准）
- [LICENSE](LICENSE) / [NOTICE](NOTICE) — PolyForm Shield 1.0.0：个人与内部自用可以，不能拿去卖或做竞品
