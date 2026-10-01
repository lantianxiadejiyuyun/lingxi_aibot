"""Deterministic slash commands available even when the model API is offline."""
from __future__ import annotations

HELP = """当前会话命令（网页、飞书通用）：
/model — 查看候选模型和当前模型
/model 模型ID或序号 — 切换模型
/think — 查看当前模型支持的思考等级
/think 等级 — 调整思考等级
/auto on|off — 开关 AI 自主切换模型与思考等级
/compact — 压缩当前上下文，保留完整聊天记录
/context 或 /status — 查看模型和上下文状态
/help 或 / — 显示帮助
手动选择模型或思考等级后会锁定选择；/auto on 可重新允许 AI 自主切换。
候选模型在「设置 → 模型与人设」配置，使用该账号当前接口和 API Key。"""


def is_command(text: str) -> bool:
    return str(text or '').lstrip().startswith(('/', '／'))


def handle_command(conversation, user, text: str) -> str | None:
    if not is_command(text):
        return None
    from app.services import model_control_service as controls
    from app.services import context_service

    parts = text.strip().replace('／', '/', 1).split()
    command = parts[0].lower()
    args = parts[1:]
    aliases = {'/模型': '/model', '/models': '/model', '/思考': '/think',
               '/reasoning': '/think', '/压缩': '/compact', '/compress': '/compact',
               '/上下文': '/context', '/状态': '/status', '/帮助': '/help'}
    command = aliases.get(command, command)
    try:
        state = controls.chat_controls(conversation, user)
        if command in ('/', '/help'):
            return HELP
        if command == '/model':
            if not args:
                choices = '\n'.join(f"{i}. {item['id']}" for i, item in enumerate(state['models'], 1))
                return f"当前模型：{state['model']}\n候选模型：\n{choices}\n发送 /model 模型ID或序号 切换，仅影响本会话。"
            if len(args) != 1:
                return '用法：/model 模型ID或序号'
            model = args[0]
            if model.lower() == 'auto':
                controls.set_auto_switch(conversation, user, True)
                return '已开启本会话 AI 自主切换，仅在你的候选模型及支持的思考等级中选择。'
            if model.lower() == 'default':
                model = controls._base_config()['model']
            if model.isdigit():
                index = int(model) - 1
                if not 0 <= index < len(state['models']):
                    return '模型序号无效，请用 /model 查看列表。'
                model = state['models'][index]['id']
            result = controls.update_chat_model(conversation, user, model=model)
            return result['message'] + '\n已锁定手动选择；/auto on 可允许 AI 自主切换。'
        if command == '/think':
            levels = ' / '.join(state['reasoning_levels'])
            if not args:
                return f"模型：{state['model']}\n当前思考等级：{state['reasoning_effort']}\n支持：{levels}\n用法：/think 等级。default 表示使用模型默认行为。"
            if len(args) != 1:
                return '用法：/think 等级'
            result = controls.update_chat_model(conversation, user, reasoning_effort=args[0])
            return result['message'] + '\n已锁定手动选择；/auto on 可允许 AI 自主切换。'
        if command == '/auto':
            if not args:
                return 'AI 自主切换：' + ('开启' if state['auto_switch'] else '关闭') + '\n用法：/auto on 或 /auto off'
            if len(args) != 1 or args[0].lower() not in ('on', 'off', '开', '关'):
                return '用法：/auto on 或 /auto off'
            enabled = args[0].lower() in ('on', '开')
            controls.set_auto_switch(conversation, user, enabled)
            return '本会话 AI 自主切换已' + ('开启。' if enabled else '关闭，保持当前模型和思考等级。')
        if command == '/compact':
            if args:
                return '用法：/compact（仅压缩当前会话的上下文）'
            result = context_service.compact_conversation(conversation, user, force=True)
            return result['message']
        if command in ('/status', '/context'):
            ctx = state['context']
            return (f"模型：{state['model']}\n思考等级：{state['reasoning_effort']}\n"
                    f"AI 自主切换：{'开启' if state['auto_switch'] else '关闭'}\n"
                    f"历史消息：{ctx['total_messages']} 条；活跃上下文：{ctx['active_messages']} 条\n"
                    f"摘要：{ctx['summary_characters']} 字；上下文约 {ctx['estimated_tokens']} tokens（字符估算，不含系统提示和工具）\n"
                    '可发送 /compact 压缩，聊天记录会完整保留。')
        return '未知命令。发送 /help 查看模型、思考等级和上下文压缩命令。'
    except ValueError as exc:
        return str(exc)
