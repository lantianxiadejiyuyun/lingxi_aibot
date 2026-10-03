# 灵犀接入 API v1

本版本已部署到 fnOS，并通过本地测试、导航站 Node→Flask 联调及线上 Gunicorn 的 HTTP/SSE/WebSocket 验证。此文档是导航站与灵犀的接入契约。

## 地址与账号

现有 fnOS 后台地址为 `http://192.168.100.72:8000/lingxi`。接口基地址为
`http://192.168.100.72:8000/lingxi/api/v1`，WebSocket 为
`ws://192.168.100.72:8000/lingxi/api/v1/ws`。

`/lingxi` 是可配置的 `ADMIN_ENTRY`，不是接口版本的一部分。没有配置入口时基地址为 `/api/v1`。
独立公开网页端口（通常 8080）仅提供公开网页与图片，不提供这些 API。公网接入需要将后端 8000 端口通过 HTTPS/WSS 反向代理暴露给导航站后端。

2026-10-03 已上线两个公网接入地址：

- `https://index.eugenstudio.cn/lingxi-service/api/v1`
- `https://ojjlab.eugenstudio.cn/lingxi-service/api/v1`

WebSocket 将 `https` 改为 `wss` 并追加 `/ws`。两个入口的 `/me` 无 Token 实测返回 401，WSS 升级及首帧鉴权通过；fnOS 容器到两个站点的保险库回调均可达，无 Token 返回 401，未读取真实保险库。
这些入口只反代 API，经云端回环 SSH 隧道转入 fnOS 8000，不公开后台管理页面。链路见 [导航站联调网络](导航站联调网络.md)。原 GOST `30079 → 8080` 继续提供公开网页，不承载接入 API。

每位用户在「设置 → 账号」生成自己的 API Token。导航站 Node 后端按已登录的导航站账号保存对应 Token，之后代理请求；无需再登录灵犀，也不共享灵犀登录密码。
每次调用携带 `Authorization: Bearer lx_…`，也兼容 `X-API-Token`。不接受 URL 查询参数中的 Token，不以浏览器登录 Cookie 代替 Token。
Token 重置或撤销后需重新绑定。旧 `.env API_TOKEN` 仍兼容管理员账号，不应拿它为多个导航站用户共享绑定。

```js
// 仅在导航站 Node 后端执行，token 来自当前导航站账号的服务端绑定记录。
const base = 'http://192.168.100.72:8000/lingxi/api/v1';
const response = await fetch(`${base}/me`, {
  headers: { Authorization: `Bearer ${token}` }
});
const result = await response.json();
// result.data.id 是应保存的灵犀账号 ID；Token 不返回浏览器。
```

## 统一约定

- 成功：`{"ok":true,"data":...}`。列表另有 `pagination:{limit,offset,total}`，默认 limit=50，上限 200。
- 失败：`{"ok":false,"error":"可读说明","code":"错误标识"}`，同时返回正确 HTTP 状态码。
- 401 表示 Token 失效；404 也用于其他账号的资源；400 表示参数错误；413 表示请求体过大。
- JSON 请求体必须是对象，最多 1 MiB；日期时间必须带时区，例如 `2026-10-03T09:00:00+08:00`。响应统一输出 UTC `Z`。
- 日期范围为左闭右开 `[start,end)`。不要直接传日期字符串；按 `/me` 的账号时区计算当地午夜并带偏移量发送。
- 所有查询和修改仅针对 Token 所属账号，管理员 Token 也不会跨账号查询。
- HTTP API 不设置跨域通配规则；建议浏览器只调用导航站自己的后端代理。

`GET /me` 示例：`{"ok":true,"data":{"id":1,"username":"king","timezone":"Asia/Shanghai","api_version":"v1","capabilities":["events","event_occurrences","tasks","conversations","chat","sse","websocket","navigation_vault"],"websocket_path":"/lingxi/api/v1/ws"}}`。

若外部反代将入口改为 `/lingxi-service`，客户端从绑定的基地址构造 `/api/v1/ws`，不要直接照搬 `/me.websocket_path`：该字段表示后端当前入口路径。

## 账号、日程和待办

| 方法与路径 | 用途 |
|---|---|
| `GET /me` | 返回账号 id、username、timezone 及接口能力 |
| `GET /tasks` | 分页待办；支持 status、q、due_after、due_before |
| `POST /tasks` | 新建待办 |
| `GET/PATCH/DELETE /tasks/{id}` | 查询、部分更新、软删除待办 |
| `GET /events` | 分页日程主记录；start、end 按主记录时间重叠过滤 |
| `POST /events` | 新建日程 |
| `GET/PATCH/DELETE /events/{id}` | 查询、部分更新、软删除日程 |
| `GET /events/occurrences?start=...&end=...` | 日历实际发生项，展开重复规则，区间最多 93 天 |

待办字段：`title`、`notes`、`due_at`（可为 null）、`priority`（1–3）、`status`（open/done/cancelled）、`project`、`tags`（字符串数组）。导航站 deadline 组件对应 `due_at`；PATCH `due_at:null` 可清除截止时间。

```json
{"title":"提交方案","due_at":"2026-10-05T18:00:00+08:00","priority":3,"tags":["工作"]}
```

日程字段：`title`、`description`、`location`、`start_at`、`end_at`（可为 null）、`all_day`、`rrule`、`reminder_minutes`。全天日程也使用账号当地午夜的带时区时间，end_at 为不包含的结束时间。

```json
{"title":"项目例会","start_at":"2026-10-05T10:00:00+08:00","end_at":"2026-10-05T11:00:00+08:00","all_day":false,"reminder_minutes":15}
```

重复日程支持 DAILY/WEEKLY/MONTHLY/YEARLY，以及受限 INTERVAL、COUNT、UNTIL、BYDAY、BYMONTHDAY、BYMONTH、WKST。不接受分钟级高频规则。
日历发生项返回 `event_id` 和 `occurrence_id`；修改使用 event_id，作用于整个系列。单次例外编辑暂不支持。超过展开上限时返回错误，不静默丢弃结果。

## AI 对话

| 方法与路径 | 用途 |
|---|---|
| `GET/POST /conversations` | 分页查询／新建会话，创建可传 title |
| `GET/PATCH/DELETE /conversations/{id}` | 查询、修改标题、删除当前账号会话 |
| `GET /conversations/{id}/messages` | 分页历史，按消息 ID 升序 |
| `POST /conversations/{id}/messages` | 发送消息，支持 stream |
| `POST /chat` | 发送消息；未提供 conversation_id 时新建会话 |

```json
{"conversation_id":123,"message":"列出明天的日程","stream":true,"request_id":"nav-001"}
```

message 最多 100000 字符。有效但不属于当前账号的 conversation_id 返回 404，不创建替代会话。
会话与灵犀网页／飞书共用同一套模型配置和 AI 工具，支持 `/help`、`/profile`、`/model` 等命令；AI 可以依用户指令操作该账号的日程和待办。

stream=false 时返回 `data:{conversation_id,reply,events}`；对话执行失败为 HTTP 502、ok=false，并在 data 保留 conversation_id 和事件供查询历史。stream=true 使用 POST 返回 SSE（浏览器应使用 fetch 流读取，不能直接用 EventSource 发 POST）。

```text
event: delta
data: {"type":"delta","conversation_id":123,"request_id":"nav-001","seq":2,"data":"明天有"}

```

统一事件字段：`type`、`conversation_id`、`request_id`、`seq`、`data`。事件类型为 start、ack、delta、tool、notice、title、error、done。
delta 是增量文字；done.data 是完整最终回复，应替换临时拼接的内容。tool.data 是工具结果对象，其余文字事件 data 为字符串；start.data 为对象。
error 后仍有 done 作为结束标识，此时不可按成功展示。request_id 仅用于关联，不提供幂等或断点续传；断线后先查询历史，避免自动重发造成重复执行。

## WebSocket

路径 `/api/v1/ws`，同样包含部署的后台入口前缀。连接后 10 秒内发送首帧：

```json
{"type":"auth","token":"lx_当前账号Token"}
```

服务端返回：

```json
{"type":"ready","data":{"user_id":1,"username":"king","protocol":"lingxi.v1"}}
```

随后发送：

```json
{"type":"chat.send","request_id":"nav-001","conversation_id":123,"message":"今天还有哪些待办？"}
```

服务端按上述 SSE 相同 JSON 事件返回。一条连接顺序执行对话；日程／待办 CRUD 使用 REST。
`{"type":"ping","request_id":"p1"}` 返回 pong。支持协议级心跳；无效或撤销 Token 会断开连接，空闲撤销最多约 30 秒检测。
客户端收到关闭后重新建立连接并鉴权，不自动重放 chat.send。Node 代理需要将上下游断开关联起来并处理背压。

命令尚未开始时的校验失败单独返回 `{"type":"error","request_id":"nav-001","conversation_id":null,"seq":0,"data":{"code":"not_found","error":"会话不存在"}}`，不追加 done；连接仍可继续使用。鉴权失败返回 error 并以 1008 关闭，连接数超限以 1013 关闭。
每用户最多 3 条连接、单进程最多 16 条（包括等待鉴权），消息最多 512 KiB。seq 从每轮聊天的 1 开始，不是跨连接序号。

### 运行与反向代理

Docker 使用单 Gunicorn worker、32 线程，为 WebSocket 连接保留容量，避免多 worker 重复运行调度器。开发 `python run.py` 使用 Werkzeug 也支持 WebSocket；Waitress 仅支持 REST/SSE，不能承载此 WebSocket。
参见 [Flask-Sock 部署说明](https://flask-sock.readthedocs.io/en/latest/web_servers.html) 和 [心跳配置](https://flask-sock.readthedocs.io/en/latest/quickstart.html)。

Nginx 代理示例（需要有效证书；域名和上游地址按部署替换）：

```nginx
location /lingxi/api/v1/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 600s;
}
```

外部代理到 fnOS 需要经过已配置的私网通道或穿透上游；云服务器上的 `127.0.0.1` 不是 fnOS。

## 导航站保险库

本轮提供回调登记、状态、撤销、按需搜索及单条查看。原插件保险库主密码与加密 blob 均不交给灵犀；由导航站按其读取开关和插件授权状态提供可撤销的授权副本。

`GET/PUT/DELETE /integrations/navigation-vault` 均使用当前灵犀账号 API Token。导航站后端负责自动登记／轮换，无需用户复制第二个 Token。

```json
{
  "read_url":"https://index.eugenstudio.cn/api/lingxi/vault/service/read",
  "read_token":"独立的受限服务端Token",
  "expires_at":"2026-10-04T01:00:00Z",
  "scope":"all",
  "navigation_user_id":"42"
}
```

PUT 要求全部五个字段。expires_at 推荐 Unix 毫秒整数（例如 Node 的 `Date.now() + 23 * 60 * 60 * 1000`），也兼容示例中的 RFC3339 字符串；过期时间必须在未来且最多 24 小时。
read_url 仅允许 HTTPS、不显式指定端口、固定 `/api/lingxi/vault/service/read` 路径，禁止凭据、查询串与片段；默认主机允许 index.eugenstudio.cn 和 ojjlab.eugenstudio.cn，并校验 DNS 为公网地址。
read_token 用服务器密钥加密保存，绑定灵犀账号、导航账号、地址、期限与 scope；响应永不回显 Token。GET/PUT 仅返回 `data:{configured,navigation_user_id,expires_at,scope}`，其中 navigation_user_id 为字符串、expires_at 统一为 Unix 毫秒整数。无配置、过期或密文无效时 configured=false，其余三项为 null。DELETE 幂等删除当前账号登记并返回同样的空状态。服务器 SECRET_KEY 变化后必须重新登记。

导航站自身须将 read_token 绑定导航用户和灵犀账号，不能仅信任回调请求体中的用户 ID；关闭授权后立即吊销 Token、删除授权副本，并通知灵犀 DELETE。
### 条目列表与单条查看

| 方法与路径 | 请求与响应 |
|---|---|
| `GET /integrations/navigation-vault/items?q=&limit=50&offset=0` | 当前账号已授权条目；data 为元数据数组，附 pagination |
| `POST /integrations/navigation-vault/reveal` | `{ "id":"条目字符串ID" }`，只返回选中一条的 id/title/site/username/password |

列表元数据只有 `id`、`title`、`site`（网址 origin）、`username_masked`、`password_set`；不包含密码、完整用户名、备注或网址中的路径参数。条目 ID 是字符串，可为 UUID，不要转换成整数。
导航站 Node 代理应放行上述两个子路径，并仅在用户点击查看时调用 reveal；不得将 reveal 响应送入聊天消息或第三方模型。列表 limit 上限 200，AI 搜索工具最多取 20 条。

每个灵犀账号当前只保存一份导航保险库来源；另一导航站重新登记会替换该来源。这不影响多个导航站同时接入同一灵犀账号的日程、待办和聊天。

上述 items 和 reveal 接口支持可选请求头 `X-Navigation-Vault-Grant`，值为登记时 `read_token` 的 SHA-256 十六进制摘要（64 位）。导航站 Node 后端应从当前绑定的 `service_token_hash` 构造此头，不接受浏览器传入的摘要。灵犀在读取最新登记后、调用导航回调前核对摘要；来源被替换、Token 已轮换而摘要未更新或头格式无效时，返回 HTTP 409、`code:"vault_source_changed"`，不调用任何来源的回调。收到此错误应提示重新绑定当前站点的授权，不自动移除此头后重试。

```js
// 导航站后端：serviceTokenHash 来自当前登录账号的服务端授权记录。
const headers = {
  Authorization: `Bearer ${lingxiToken}`,
  'X-Navigation-Vault-Grant': serviceTokenHash
};
```

不传此头时保持兼容，读取该灵犀账号当前登记的来源；灵犀自身网页和 AI 元数据搜索沿用此行为。该摘要是来源一致性检查，不能替代 API Token 鉴权；回调完成后的授权变更检查仍然执行。

灵犀「设置 → 账号」提供保险库查看入口，网页 Cookie 登录与 CSRF 保护仍然生效。AI 的 `search_navigation_vault` 工具只搜索元数据并返回此受保护页面链接，没有向模型返回密码的工具。页面明文只在用户点击后临时显示，隐藏页面或超时后清空。

该工具同时返回 `action:{type:"open_navigation_vault",list_path:"/integrations/navigation-vault/items",reveal_path:"/integrations/navigation-vault/reveal"}`。导航站应根据工具名或此 action 打开本地保险库面板；`url_scope:"lingxi_web"` 表示 url 仅供灵犀网页使用，不要将管理页面也公开反代。
现有流式 tool 事件的 result 是最多 200 字符的摘要，可能不是完整 JSON；导航站应优先根据 `data.name === "search_navigation_vault" && data.ok` 显示“打开保险库”按钮，并调用上表中的固定路径。不要尝试从截断字符串提取密码或完整 action。

每次读取均校验登记有效期，重新解析并验证全部 DNS 地址，再将 HTTPS 连接固定到已校验公网 IP；禁用代理环境变量和重定向，限制响应为 2 MiB、最多 10000 个条目。没有密码缓存，也不写入数据库、日志或聊天历史。
导航回调协议为 `POST read_url`、`Authorization: Bearer read_token`。列表请求 `{}`，单条请求 `{"ids":["条目ID"]}`；读取后还会重新检查登记，撤销或轮换后丢弃在途结果。

导航站回调响应约定：`{items:[{id,title,url,username,password,notes}],snapshot_version,source_version,updated_at}`。灵犀只提取对应入口允许返回的字段。远端 401/403、409、503 分别表示授权失效、副本需更新或服务不可用，灵犀返回脱敏错误，不透传原始响应正文。

## 隔离联调入口

运行以下脚本会启动真实 Flask 路由与独立临时 SQLite，创建两个合成账号，并将合成凭据写入指定目录的 `ready.json`。目录必须尚不存在；所有上游 HTTP 被禁用，聊天使用 `/help`，不调用真实模型或密码库：

```powershell
.venv/Scripts/python.exe scripts/tests/serve_integration_fixture.py --directory "$env:TEMP/lingxi-integration-fixture" --port 19400
```

ready.json 包含 base_url、api_base、ws_url 及 users（id、username、password、api_token）。服务仅监听 127.0.0.1，完成联调后停止进程。Python 回归入口为 `python -m unittest discover -s scripts/tests -v`，包括真实本机 WebSocket、SSE 与 Cookie/Token 隔离测试。

2026-10-03 本地验证：Python 共 305 项，304 通过、1 项因 Windows 符号链接权限跳过；Node 入口回归 10 项通过；真实 Chrome 桌面／390px 手机检查 16 项通过。外部模型及密码回调测试均使用隔离数据，未读取真实密码。Linux Gunicorn 和跨服务器隧道仍需在联合发布时验证。

导航站真实 Node→Flask 联调 8 组通过：账号绑定、重复日程、待办截止日期、会话 CRUD、SSE、WebSocket、跨账号隔离及合成数据清理。SSE/WS 均使用真实 `/help` 回复，不调用模型。
