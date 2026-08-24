---
name: edit-flow
description: >
  灵犀项目改功能 / 修 bug / 改页面或接口的固定流程：先读源码，再多次提问确认需求，
  然后改代码，跑对应测试，测试通过后同步 docs/PROJECT.md、README.md、docs/使用说明.html。
  Use when adding features, fixing bugs, changing UI/routing/setup/login/pages, or the user
  says 改一下 / 修一下 / 加个功能 / 编辑流程, or runs /edit-flow.
---

# 灵犀编辑流程

改**功能、缺陷、页面、接口、安装/登录/访问方式**时必须走完下面顺序。错别字、注释、纯格式可直接改，不必提问和测全套。

未确认需求前不要改业务代码。测试未通过不要更新说明文档。

## 1. 先看源码

用 `grep` / `read_file` / `list_dir` 定位现状，不要凭记忆写。至少看清：

- 相关 blueprint / service / 模板
- 现有测试 `scripts/test_*.py`
- 三份说明里对应段落（`docs/PROJECT.md`、`README.md`、`docs/使用说明.html`）

读完后用一两句话向用户复述「现在代码是怎么做的」。

## 2. 问清需求（至少两轮）

用 `ask_user_question`，**不要一轮问完就动手**。

第一轮：改什么、不改什么、成功长什么样。  
第二轮：边界（空值/未登录/局域网/短入口/旧链接）、失败时怎么表现。

用户已经在对话里写死的选项不要再问。仍含糊就继续问，直到可执行。

## 3. 改代码

只改确认过的范围。保持项目现有风格；不要顺手重构无关文件。

## 4. 测试

用仓库 `.venv`：

```powershell
.\.venv\Scripts\python.exe scripts\<对应测试>.py
```

按改动选脚本（可多跑）：

| 改动 | 脚本 |
|---|---|
| 安装向导 / 令牌 / 短入口登录 | `test_setup.py`（进程内部分即可；活服务项可跳过） |
| 网页路径 / PAGE_PORT / 公开页 | `test_page_port.py`；有活服务再跑 `test_pages.py` |
| 设置页 | `test_ai_settings.py` / `test_backup_settings.py` / `test_theme.py` 等对应项 |
| 登录 / 主题 | `test_theme.py` |
| 飞书 | `test_feishu_bot.py`、`test_feishu_receive.py` |
| 大范围或说不清 | 先跑对应单测，再视情况 `smoke_test.py`（需 `http://127.0.0.1:5000`） |

失败：修到通过，或写明哪一项因环境（无 MySQL / 无活服务）没跑、为什么。  
**禁止**在失败时声称已完成。

## 5. 测试通过后更新说明

三份都打开核对，有用户可见变化的段落必须改：

1. `docs/PROJECT.md` — 对应章节 + 「16. 变更记录」加一行（日期用当天）
2. `README.md` — 功能表、访问方式、结构、测试列表若相关则改
3. `docs/使用说明.html` — 对应卡片（安装/登录/网页/设置/安全）

没改到的段落保持原样，不要为了「三份都动」去写空话。

访问方式以代码为准：后台 `/<ADMIN_ENTRY>/login`，公开网页 `/webs/html/<slug>`，不再按双域名分流。
