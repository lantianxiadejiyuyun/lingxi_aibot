# 灵犀 AiBot 部署指南（Ubuntu + 宝塔面板 + Nginx）

> **请改用仓库文档 [docs/部署.md](../docs/部署.md)**（与当前代码同步：网页端口、飞书 SDK、13 个内置任务、Docker MySQL）。下文是旧版归档，可能过时。

> 本指南是**方式一：宝塔 + Supervisor**（与你的 Ubuntu + 宝塔 + Nginx 环境最贴合，推荐）。
> 另提供**方式二：Docker 一键部署**（完整自包含，或只跑 app 容器接宝塔的 MySQL/Nginx），见 [README-Docker.md](README-Docker.md)。

> 环境：Ubuntu 20.04/22.04 + 宝塔面板（BT Panel）+ Nginx + MySQL 8.0
> 架构：Nginx 终结 HTTPS → 反代 127.0.0.1:8000 的 waitress（单进程多线程，避免定时任务重复执行）
> 域名：`admin.eugenstudio.cn` 后台 / `web.eugenstudio.cn` AI 网页（主域名 eugenstudio.cn 留作其他业务，不处理）

```
浏览器 ──HTTPS──> Nginx (80/443)
                    │  proxy_pass http://127.0.0.1:8000
                    ▼
              waitress (wsgi:app)  ← Supervisor 守护
                    │
                    ▼
             MySQL 8.0 (127.0.0.1:3306)
```

---

## 0. 前置准备

1. 服务器安装**宝塔面板**（https://www.bt.cn），并在软件商店安装：
   - Nginx 1.24+（或面板自带）
   - MySQL 8.0
   - **Supervisor 管理器**（或「Python 项目管理器」，二选一，见第 6 步）
2. 域名解析（DNS 面板操作）：
   - `admin.eugenstudio.cn` → 服务器公网 IP（A 记录）
   - `web.eugenstudio.cn` → 服务器公网 IP
   - 主域名 `eugenstudio.cn` **不要**指向本应用
3. 本地把 `deploy/` 目录打成 zip 上传（或直接在面板文件管理上传 `deploy.zip` 后解压）。

---

## 1. 上传代码

把 `deploy/` 里的内容放到站点根目录 `/www/wwwroot/aibot/`，最终结构：

```
/www/wwwroot/aibot/
├── app/                  # 应用代码（不要放 data/、.env、venv 进去）
├── nginx/                # 参考配置（admin/web 两个域名）
├── supervisor/           # 进程守护参考配置
├── run.py / wsgi.py
├── requirements.txt
├── .env.example
└── README-部署.md
```

设置属主（宝塔运行用户一般是 www）：

```bash
chown -R www:www /www/wwwroot/aibot
```

---

## 2. 创建数据库（宝塔面板 → 数据库）

1. 点「添加数据库」：
   - 数据库名：`ai_bot`（字符集 **utf8mb4**，排序规则 utf8mb4_unicode_ci）
   - 用户名：`ai_bot`，密码：自己生成一个强密码
   - 访问权限：**本地服务器**
2. 记下用户名和密码，第 4 步填进 `.env`。
   （如果用 root 也可以，但建议独立账号。）

---

## 3. 安装 Python 依赖

宝塔软件商店安装「Python 项目管理器」（自带 Python 3.11），或使用系统 Python 3.11：

```bash
cd /www/wwwroot/aibot

# 创建虚拟环境（Python 项目管理器创建项目时也会自动建 venv，路径通常是 /www/wwwroot/aibot/venv）
/usr/bin/python3.11 -m venv venv      # 具体路径以你环境为准

# 安装依赖
./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt
```

> 国内服务器 pip 慢可加镜像：`./venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

---

## 4. 配置环境变量（可跳过）

```bash
cd /www/wwwroot/aibot
cp .env.example .env
nano .env        # 或宝塔文件编辑器
```

**也可以完全跳过本步**：应用启动时若发现项目根没有 `.env`，会**自动生成**（复制 `.env.example`
并填入随机 SECRET_KEY），并自动跳转到网页安装向导 `/setup` 完成配置。

必改项（如自动生成后手动补充）：

| 变量 | 说明 |
|---|---|
| `SECRET_KEY` | 生成随机串：`python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DB` | **可留空**——首次启动后在网页安装向导里填写（保存自动写入 .env） |
| `LLM_API_KEY` | DeepSeek API Key |
| `ADMIN_DOMAIN` | `admin.eugenstudio.cn` |
| `PAGE_DOMAIN` | `web.eugenstudio.cn` |
| `ADMIN_PASSWORD` | 初始管理员密码（仅网页向导自动建管理员时用，也可在向导里手动创建） |

> 后台/网页域名也可以在登录后「设置 → 域名设置」里修改，二选一即可。
> 图片生成、视觉、语音、向量、联网搜索等 Key 可先留空，登录后在设置页配置。
> ⚠️ **MySQL 三项即使不填也能正常启动**：应用会在数据库不可用时自动进入安装向导模式，
> 不会因连不上库而崩溃（日志里出现「调度器启动失败」是正常现象，连上库后会自动补启动）。

---

## 5. 初始化数据库（可跳过）

```bash
cd /www/wwwroot/aibot
./venv/bin/python -m flask init-db
```

**也可以跳过本步**：首次访问网页安装向导时，填好数据库连接后会自动完成建表、
补列、内置定时任务的初始化（幂等）。命令行方式与网页方式等效，二选一即可。
重复执行安全；出错时先确认第 2、4 步的 MySQL 账号密码正确。

---

## 6. 启动服务（Supervisor 或 Python 项目管理器，二选一）

### 方案 A：Supervisor 管理器（推荐）

宝塔 → 软件商店 →「Supervisor管理器」→ 添加守护进程，填写：

- 名称：`aibot`
- 启动用户：`www`
- 运行目录：`/www/wwwroot/aibot`
- 启动命令：

```
/www/wwwroot/aibot/venv/bin/waitress-serve --host=127.0.0.1 --port=8000 --threads=8 --channel-timeout=3600 wsgi:app
```

- 日志：`/www/wwwroot/aibot/logs/`（目录不存在先 `mkdir -p /www/wwwroot/aibot/logs`）

保存后状态应为「运行中」。`deploy/supervisor/aibot.conf` 是等价的配置文件，系统级 Supervisor 可直接复制使用。

### 方案 B：Python 项目管理器

宝塔 → 软件商店 →「Python项目管理器」→ 添加项目：

- 项目路径：`/www/wwwroot/aibot`
- Python 版本：3.11
- 启动方式：**自定义启动**，命令同上
- 勾选「开机启动」「进程守护」

### 验证

```bash
curl http://127.0.0.1:8000/        # 应返回 302（跳转登录）
tail -f /www/wwwroot/aibot/logs/app.log
```

---

## 7. Nginx 站点配置（两个域名）

宝塔 → 网站 → 添加站点（分别添加两次）：

1. 域名 `admin.eugenstudio.cn`，纯静态即可（我们不托管 PHP）
2. 域名 `web.eugenstudio.cn`，同上

然后分别进入「站点设置 → 配置文件」，用 `deploy/nginx/` 下对应文件的内容**替换**（两处关键点：反代 127.0.0.1:8000 + SSE 关缓冲）：

- `deploy/nginx/admin.eugenstudio.cn.conf` → admin 站点
- `deploy/nginx/web.eugenstudio.cn.conf` → web 站点

最后「站点设置 → SSL」→ 勾选域名 → **Let's Encrypt 一键申请**并开启「强制 HTTPS」。

> ⚠️ 若网页域名下回答/内容不显示、一直转圈：99% 是 SSE 被 Nginx 缓冲，检查配置里
> `proxy_buffering off; proxy_read_timeout 3600s;` 是否生效（改完 `nginx -s reload`）。

---

## 8. 网页安装向导（建库 + 建管理员）

浏览器打开 `https://admin.eugenstudio.cn/setup`

系统未初始化时（无 .env / 数据库未配置 / 未建表 / 无管理员），**任意页面都会自动跳转到**
`/setup` 安装向导（已初始化后自动跳转登录/仪表盘），分三步：

1. **① 配置数据库**：填写 MySQL 主机/端口/用户/密码/库名 → 「测试连接」→「保存配置」
   （保存后写入项目 `.env` 并**立即生效，无需重启**；密码留空则保留原配置）
2. **② 初始化数据库**：点击「开始初始化」，自动建表、补列、写入 10 个内置定时任务
3. **③ 创建管理员**：填写用户名/密码，创建后自动跳转登录

> 安装接口仅在未初始化时开放（防误改 .env）；初始化完成后 `/setup` 自动失效。
> 向导填写的 MySQL 必须已创建好（宝塔「数据库」页新建，字符集 utf8mb4），向导不负责建库。

登录后进「设置」：
- 确认「域名设置」已显示 admin/web 两个域名
- 配置通知渠道、联网搜索、图片生成、语音、向量等 Key
- 修改管理员密码

---

## 9. 上线前安全清单

- [ ] `.env` 的 `SECRET_KEY` 是随机 64 位 hex，且**不对外泄露**
- [ ] `SESSION_COOKIE_SECURE=1`（已在模板中默认开启，HTTPS 下生效）
- [ ] MySQL 数据库账号非 root、强密码、仅本地访问
- [ ] 服务器防火墙（宝塔安全组）只放行 80/443/宝塔面板端口，**8000 端口不对公网开放**
- [ ] 域名解析正确，`https://admin.eugenstudio.cn` 证书有效（浏览器绿锁）
- [ ] 网页域名测试：`https://web.eugenstudio.cn/<某个已发布网页slug>` 可公开访问，`https://web.eugenstudio.cn/` 根路径返回 404（不暴露后台）
- [ ] 定时任务只在一个进程里跑（waitress 单进程多线程，勿再用 gunicorn -w 多 worker）

---

## 10. 日常运维

```bash
# 重启应用
supervisorctl restart aibot          # 或宝塔 Supervisor 面板点「重启」

# 看日志
tail -f /www/wwwroot/aibot/logs/app.log

# 更新代码（每次改动后）
# 1) 本地重新打包 deploy/（只覆盖 app/、wsgi.py、requirements.txt 等，不要覆盖 .env、data/、venv）
# 2) 上传替换 /www/wwwroot/aibot 下对应文件
# 3) ./venv/bin/pip install -r requirements.txt   （依赖有变时）
# 4) ./venv/bin/python -m flask init-db            （新增数据库列时，幂等可重复）
# 5) supervisorctl restart aibot

# 数据备份（应用内「设置 → 备份」会备份数据库+数据文件到 data/backups/，可下载）
# 建议另用宝塔「计划任务」定期 mysqldump：
#   mysqldump -uai_bot -p'密码' ai_bot > /www/backup/ai_bot_$(date +%F).sql
```

---

## 11. 常见问题排查

| 现象 | 原因 / 解决 |
|---|---|
| 502 Bad Gateway | waitress 没起来：`supervisorctl status`、看 app.err.log；或改了端口没同步 Nginx |
| 回答一直转圈/空白 | Nginx 缓冲了 SSE：`proxy_buffering off` + `proxy_read_timeout 3600s`，reload 后重试 |
| 登录后马上被登出 | `SESSION_COOKIE_SECURE=1` 但用了 http 访问 → 强制 HTTPS 后正常 |
| 提示「请使用后台域名访问」 | `.env` 的 `ADMIN_DOMAIN` 与访问域名不一致，或 Nginx `proxy_set_header Host $host` 缺失 |
| `flask init-db` 连不上 MySQL | 检查 `.env` 账号密码、MySQL 是否仅 localhost、宝塔 MySQL 是否启动 |
| 定时任务重复执行 | 用了多 worker 启动（gunicorn -w 4 等）→ 改回 waitress 单进程多线程 |
| 访问 `web.eugenstudio.cn/` 显示了后台 | `PAGE_DOMAIN` 未设置或与访问域名不一致（设置页「域名设置」核对） |
| 登录提示频繁（429） | 登录限流 10 次/分钟，等一分钟或重启服务清空内存计数 |

---

## 目录速查

| 路径 | 内容 |
|---|---|
| `/www/wwwroot/aibot/app/` | 应用代码（blueprints/services/ai/models/static/templates） |
| `/www/wwwroot/aibot/.env` | 环境变量（含密钥，勿提交/备份到公网） |
| `/www/wwwroot/aibot/data/` | 运行时数据：`backups/` 备份、`images/` 生成图片 |
| `/www/wwwroot/aibot/logs/` | 运行日志 |
| `/www/wwwroot/aibot/venv/` | Python 虚拟环境 |
