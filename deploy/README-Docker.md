# 灵犀 AiBot — Docker 部署指南（方式二，可选）

> **请改用仓库文档 [docs/部署.md](../docs/部署.md)** 第六节（与当前 compose / 网页端口 / MySQL profile 同步）。下文是旧版归档。
>
> 宝塔方案见仓库 [docs/部署.md](../docs/部署.md) 第四节。
> 本文是**另一种选择**：Docker 一键部署。适合：
> - 干净服务器（无宝塔），想一条命令跑起来
> - 宝塔环境但想让应用容器化（MySQL/Nginx 仍可用宝塔的，见「方式 B」）

```
浏览器 ──HTTPS──> nginx 容器 (80/443)
                    │  proxy_pass http://app:8000
                    ▼
              app 容器（waitress 单进程多线程）
                    │
                    ▼
              db 容器（MySQL 8.0，数据在 mysql_data 卷）
```

---

## 方式 A：完整自包含（app + mysql + nginx）

### 1. 准备

服务器安装 Docker Engine + Compose 插件：

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker compose version    # 应输出 v2.x
```

域名 DNS：`admin.eugenstudio.cn`、`web.eugenstudio.cn` 都解析到服务器公网 IP。

### 2. 配置 .env

```bash
cd deploy    # 部署包目录
cp .env.example .env
nano .env
```

必改项：

| 变量 | 说明 |
|---|---|
| `SECRET_KEY` | 随机串：`openssl rand -hex 32` |
| `MYSQL_ROOT_PASSWORD` | MySQL 容器 root 密码（**容器专用**，首次初始化后不可改） |
| `MYSQL_PASSWORD` | 应用连接 MySQL 的密码（与 root 密码不同） |
| `LLM_API_KEY` | DeepSeek API Key |
| `ADMIN_DOMAIN` / `PAGE_DOMAIN` | `admin.eugenstudio.cn` / `web.eugenstudio.cn` |
| `SESSION_COOKIE_SECURE` | **先设 `0`**（本阶段是 HTTP）；配好 SSL 后改回 `1` |

> `MYSQL_HOST=127.0.0.1` 无需改——compose 会自动把应用容器里的 `MYSQL_HOST` 覆盖为 `db`（容器服务名）。

### 3. 启动

```bash
docker compose up -d --build
```

首次启动流程：MySQL 容器健康 → `init` 容器自动执行 `flask init-db`（建表+内置任务，幂等）→ app 启动 → nginx 监听 80/443。

查看状态与日志：

```bash
docker compose ps
docker compose logs -f app      # 应用日志
docker compose logs init        # 初始化结果（看到「数据库初始化完成 ✓」即成功）
```

### 4. 初始化管理员并登录

浏览器访问 `http://admin.eugenstudio.cn/setup` → **可视化安装向导**：
① 配置数据库（填 `db` 服务创建的用户/库，即 `.env` 里的 `MYSQL_USER`/`MYSQL_DB`，密码为 `MYSQL_PASSWORD`）
→ ② 开始初始化（自动建表，也可跳过——compose 的 init 容器已自动执行过）→ ③ 创建管理员 → 自动跳登录。

（域名解析生效前可用 `http://服务器IP/setup` 先访问，但**后台域名守卫**可能拦截——此时先在 .env 留空 `ADMIN_DOMAIN`，或用 `--host` 绑定测试后改回。推荐直接配好 DNS。）

### 5. 配置 HTTPS（重要）

1. 获取证书（`admin` 与 `web` 两个域名，可用 `acme.sh` / certbot / 宝塔申请的证书），
   把 `fullchain.pem`、`privkey.pem` 放进 `./certs/`（会自动挂载进 nginx 容器）
2. 编辑 `nginx/docker.conf`：取消 443 server 块注释
3. 编辑 `.env`：`SESSION_COOKIE_SECURE=1`
4. 重启：

```bash
docker compose up -d web
docker compose restart web
```

### 6. 数据持久化与备份

| 数据 | 位置 |
|---|---|
| MySQL 数据 | `mysql_data` 卷（`docker volume ls` 查看） |
| 应用数据（备份/图片） | `./data/`（宿主目录，随时可拷走） |

- 应用内「设置 → 备份」每日自动导出 JSON 到 `./data/backups/`（gzip，可下载）
- MySQL 整体备份：`docker compose exec db sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" ai_bot' > backup.sql`

### 7. 更新代码 / 重启

```bash
git pull（或重新上传 deploy/ 覆盖）   # 覆盖后注意保留 .env、data/、certs/
docker compose up -d --build          # 重建并滚动重启；init 会再跑一次（幂等，无害）
```

---

## 方式 B：宝塔 + Docker（只用 app 容器，MySQL/Nginx 用宝塔的）

适合你的宝塔环境：MySQL 用宝塔装的，Nginx 用宝塔站点配置，只有应用跑在容器里。

1. 编辑 `docker-compose.yml`：
   - **注释掉** `db`、`web`、`init` 三个服务（或用 `docker compose up app -d` 只起 app）
   - `app` 服务 environment 里 `MYSQL_HOST` 改为宿主机 IP：容器访问宿主 MySQL 一般用 `172.17.0.1`（Docker 默认网桥网关），宝塔 MySQL 若绑定了内网 IP 也可直接用那个 IP
   - `app` 服务取消 `ports: ["8000:8000"]` 注释
2. 启动：`docker compose up -d --build app`
3. 数据库初始化（连宿主 MySQL）：

```bash
docker compose run --rm -e MYSQL_HOST=172.17.0.1 app python -m flask init-db
```

> 或先建好库表再起 app。`init` 服务不需要。

4. 宝塔添加 `admin.eugenstudio.cn` / `web.eugenstudio.cn` 两个站点，Nginx 反代 `127.0.0.1:8000`
   （配置直接用 `nginx/` 下的两份 conf，SSE 参数已带好），SSL 用宝塔一键申请
5. 宝塔 MySQL 需允许 `ai_bot` 用户从 `172.17.0.1` 或内网网段连接（「数据库 → 权限 → 访问权限」选「任意」或指定内网 IP）

---

## 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| `init` 容器报错连不上 MySQL | 等 MySQL 健康再跑：`docker compose up init`；或 `docker compose logs db` 看密码是否一致 |
| 回答一直转圈/空白 | nginx 缓冲了 SSE：确认 `nginx/docker.conf` 有 `proxy_buffering off; proxy_read_timeout 3600s;`，改后 `docker compose restart web` |
| 登录后马上被登出 | `SESSION_COOKIE_SECURE=1` 但走的是 HTTP → 先设 0，配好 SSL 再设 1 |
| 提示「请使用后台域名访问」 | `ADMIN_DOMAIN` 与访问域名不一致，或 nginx `proxy_set_header Host $host` 缺失 |
| 定时任务重复执行 | 确认用镜像默认 CMD（waitress 单进程），不要自己加多 worker gunicorn |
| 80/443 被宿主其他服务占用 | `.env` 改 `WEB_HTTP_PORT` / `WEB_HTTPS_PORT`，如 `8080`/`8443`，Nginx 再映射 |
| MySQL 容器起不来 | 端口未映射不冲突；看 `docker compose logs db`；`MYSQL_ROOT_PASSWORD` 首次初始化后不能改（改了需删卷重建：`docker compose down -v`，**会丢数据**） |

---

## 目录速查（Docker 方案新增文件）

| 文件 | 作用 |
|---|---|
| `Dockerfile` | python:3.11-slim + waitress，非 root 运行 |
| `docker-compose.yml` | app / init / db / web 四服务编排 |
| `nginx/docker.conf` | 容器内 Nginx 反代（80 + 可选 443，SSE 参数） |
| `certs/` | （运行时）HTTPS 证书挂载目录 |
| `data/` | （运行时）备份与图片，宿主可见 |
