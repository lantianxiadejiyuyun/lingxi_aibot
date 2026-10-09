"""All chat transports, including Feishu, use these same durable job tools."""
from app.ai.registry import register_tool
from app.services import media_service, storage_service
from app.utils.scoping import current_user_id


@register_tool(name="list_media_storage", description="列出当前账号可用的本机硬盘/NAS存储位置，创建下载任务前选择存储ID。",
               parameters={"type": "object", "properties": {}})
def list_media_storage():
    return storage_service.list_locations(current_user_id())


@register_tool(name="download_anime", description="创建后台番剧下载任务。AI多轮搜索解析下载地址，停滞重试、失败后自动启动关联AI会话重新搜源；立即返回任务ID。下载完成通过账号默认通知渠道（可含飞书）推送。",
    parameters={"type": "object", "properties": {
        "title": {"type": "string"}, "storage_id": {"type": "integer"}, "aliases": {"type": "string"},
        "season": {"type": "string"}, "episode": {"type": "string"}, "quality": {"type": "string"},
        "subtitle": {"type": "string"}, "notes": {"type": "string"}, "request_key": {"type": "string", "description": "相同请求复用此标识避免重复任务"}},
        "required": ["title", "storage_id"]})
def download_anime(title, storage_id, aliases="", season="", episode="", quality="", subtitle="", notes="", request_key=None):
    from app.services.model_control_service import current_conversation
    payload = dict(title=title, storage_id=storage_id, aliases=aliases, season=season, episode=episode, quality=quality, subtitle=subtitle, notes=notes)
    if request_key:
        payload["request_key"] = request_key
    try:
        payload["conversation_id"] = current_conversation()[0].id
    except (ValueError, RuntimeError):
        pass
    return media_service.task_dict(media_service.create_task(current_user_id(), payload))


@register_tool(name="media_download_status", description="查看后台下载任务进度、重试次数和失败原因。可省略task_id列出最近任务。",
    parameters={"type": "object", "properties": {"task_id": {"type": "integer"}}})
def media_download_status(task_id=None):
    uid = current_user_id()
    return media_service.task_dict(media_service.get_task(uid, task_id), detail=True) if task_id else media_service.list_tasks(uid, 10)


@register_tool(name="control_media_download", description="暂停、继续、取消下载，或要求AI再次搜索资源。action: pause/resume/cancel/retry。取消保留已下载部分文件。",
    parameters={"type": "object", "properties": {"task_id": {"type": "integer"}, "action": {"type": "string", "enum": ["pause", "resume", "cancel", "retry"]}}, "required": ["task_id", "action"]})
def control_media_download(task_id, action):
    return media_service.task_dict(media_service.action(current_user_id(), task_id, action))


@register_tool(name="list_media_files", description="浏览已配置本地硬盘或NAS中的文件；使用存储ID和相对路径，不能访问存储目录外文件。",
    parameters={"type": "object", "properties": {"storage_id": {"type": "integer"}, "path": {"type": "string"}}, "required": ["storage_id"]})
def list_media_files(storage_id, path=""):
    return storage_service.list_files(current_user_id(), storage_id, path)


@register_tool(name="start_background_research", description="创建框架内部后台调查任务，支持多轮工具调用及最多三个搜索/解析/核验子代理。适合较长的联网调查或文件检索，立即返回运行ID。",
    parameters={"type": "object", "properties": {"prompt": {"type": "string"}, "request_key": {"type": "string"}}, "required": ["prompt"]})
def start_background_research(prompt, request_key=None):
    from app.services.native_agent_service import queue_agent
    from app.services.model_control_service import current_conversation
    try:
        conversation_id = current_conversation()[0].id
    except (ValueError, RuntimeError):
        conversation_id = None
    return queue_agent(current_user_id(), prompt, conversation_id=conversation_id, request_key=request_key)


@register_tool(name="background_research_status", description="读取后台调查的结果、子代理与工具步骤；返回run_id对应当前账号的记录。",
    parameters={"type": "object", "properties": {"run_id": {"type": "integer"}}, "required": ["run_id"]})
def background_research_status(run_id):
    from app.services.native_agent_service import get_run
    return get_run(current_user_id(), run_id)
