"""Tools can change only the active user's current conversation."""
from app.ai.registry import register_tool
from app.services import model_control_service as controls


@register_tool(name='get_chat_controls', description='查看当前会话模型、可选模型、支持的思考等级、AI自主切换开关与上下文状态。',
               parameters={'type': 'object', 'properties': {}, 'required': []})
def get_chat_controls():
    return controls.chat_controls(*controls.current_conversation())


@register_tool(name='switch_chat_model',
               description='按任务需要切换当前会话的模型或思考等级，下一次模型请求生效。只能用 get_chat_controls 返回的候选项；AI自主切换关闭时禁止修改。不要反复切换。',
               parameters={'type': 'object', 'properties': {
                   'model': {'type': 'string', 'description': '候选模型ID，可省略以保留当前模型'},
                   'reasoning_effort': {'type': 'string', 'description': '该模型支持的思考等级，可省略'},
                   'reason': {'type': 'string', 'description': '简短解释本次任务为什么需要切换'}}, 'required': ['reason']})
def switch_chat_model(reason: str, model=None, reasoning_effort=None):
    if not str(reason or '').strip() or len(str(reason)) > 300:
        raise ValueError('请提供 1–300 字的切换原因')
    if model is None and reasoning_effort is None:
        raise ValueError('至少指定模型或思考等级')
    result = controls.update_chat_model(*controls.current_conversation(), model=model,
                                        reasoning_effort=reasoning_effort, by_ai=True)
    result['reason'] = reason
    return result


@register_tool(name='compact_chat_context',
               description='将当前会话的较早上下文压缩为增量摘要，保留全部聊天记录和最近完整轮次；长任务需要减小上下文时使用。',
               parameters={'type': 'object', 'properties': {}, 'required': []})
def compact_chat_context():
    from app.services.context_service import compact_conversation
    return compact_conversation(*controls.current_conversation(), force=True)
