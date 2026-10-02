"""Deterministic slash commands available even when the model API is offline."""
from __future__ import annotations

HELP = """当前会话命令（网页、飞书通用）：
/profile [序号或ID] — 查看/切换聊天模型配置组（接口、Key、容量一起切换）
/prompt [序号或ID] — 查看/切换提示词组
/image [序号或ID] — 查看/切换图片生成组
/vision [序号或ID] — 查看/切换备用视觉组
/model — 查看候选模型和当前模型
/model 模型ID或序号 — 切换模型
/think — 查看当前模型支持的思考等级
/think 等级 — 调整思考等级
/auto on|off — 开关 AI 自主切换模型与思考等级
/compact — 压缩当前上下文，保留完整聊天记录
/context 或 /status — 查看模型和上下文状态
/help 或 / — 显示帮助
手动选择模型或思考等级后会锁定选择；/auto on 可重新允许 AI 自主切换。
候选模型使用所选配置组的接口和 API Key；各类配置组在设置中管理。"""


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
        if command in ('/profile', '/prompt', '/image', '/vision'):
            kind = {'/profile': 'chat', '/prompt': 'prompt', '/image': 'image', '/vision': 'vision'}[command]
            list_key = 'profiles' if kind == 'chat' else kind + '_profiles'
            current_key = 'profile_id' if kind == 'chat' else kind + '_profile_id'
            choices = state[list_key]
            if not args:
                rows = '\n'.join(f"{i}. {p['name']}{'（当前）' if p['id'] == state[current_key] else ''}" for i, p in enumerate(choices, 1))
                return f"可选配置组：\n{rows}\n发送 {command} 序号或ID 切换，仅影响本会话。"
            if len(args) != 1:
                return f'用法：{command} 序号或ID'
            chosen = args[0]
            if chosen.isdigit():
                index = int(chosen) - 1
                if not 0 <= index < len(choices):
                    return '配置组序号无效，请先查看列表。'
                chosen = choices[index]['id']
            return controls.update_chat_profile(conversation, user, kind, chosen)['message']
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
                model = controls._base_config(conversation)['model']
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
            window_text = '未指定，请按模型实际容量在设置中填写' if ctx.get('legacy_context_policy') else f"{ctx.get('context_window_tokens', 0):,} tokens（按所选配置）"
            return (f"模型：{state['model']}\n思考等级：{state['reasoning_effort']}\n"
                    f"AI 自主切换：{'开启' if state['auto_switch'] else '关闭'}\n"
                    f"历史消息：{ctx['total_messages']} 条；活跃上下文：{ctx['active_messages']} 条\n"
                    f"模型窗口：{window_text}\n"
                    f"摘要：{ctx['summary_characters']} 字；历史上下文约 {ctx['estimated_tokens']} tokens（估算）\n"
                    '可发送 /compact 压缩，聊天记录会完整保留。')
        return '未知命令。发送 /help 查看模型、思考等级和上下文压缩命令。'
    except ValueError as exc:
        return str(exc)
