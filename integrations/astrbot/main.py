"""AstrBot transport for Lingxi; the AI and task tools stay in Lingxi."""
from __future__ import annotations

import asyncio
from contextlib import suppress

import httpx
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from .bridge import Bridge, BridgeError, Client, parse_accounts


HELP = ("灵犀桥接：\n/lingxi 身份：查看当前私聊标识\n/lingxi 绑定：连接后台配置的灵犀账号"
        "\n/lingxi 状态：查看绑定状态\n/lingxi 解绑：停止本私聊的桥接和主动推送"
        "\n绑定后直接聊天；模型与人设在灵犀网页中设置。")


class LingxiBridgePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.http = None
        self.bridges = {}
        self.poll_task = None

    async def initialize(self):
        if not self.config.get("enabled", True):
            return
        accounts = parse_accounts(self.config)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10), trust_env=False)
        for account in accounts:
            async def load(key=account.storage_key):
                return await self.get_kv_data(key, {})

            async def save(value, key=account.storage_key):
                await self.put_kv_data(key, value)

            self.bridges[account.session] = Bridge(Client(account, self.http), load, save)
        self.poll_task = asyncio.create_task(self._poll_loop())

    async def terminate(self):
        if self.poll_task:
            self.poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.poll_task
        if self.http:
            await self.http.aclose()

    async def _send_proactive(self, session, text):
        platform = self.context.get_platform_inst(session.split(":", 1)[0])
        if platform is None:
            return False
        metadata = platform.meta()
        # QQ Official can silently skip when its cached reply message ID expires.
        # Its transport result cannot prove that an unsolicited message was sent.
        if not metadata.support_proactive_message or metadata.name == "qq_official":
            return False
        return await self.context.send_message(session, MessageChain().message(text).use_t2i(False))

    async def _poll_loop(self):
        try:
            interval = max(5, min(300, int(self.config.get("poll_interval_seconds", 15))))
        except (ValueError, TypeError):
            interval = 15
        while True:
            for bridge in self.bridges.values():
                try:
                    await bridge.poll(self._send_proactive)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Deliberately omit the exception string and upstream response.
                    self.logger.warning("灵犀主动消息暂未完成（%s），稍后重试", type(exc).__name__)
            await asyncio.sleep(interval)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def private_message(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True) or not event.is_private_chat():
            return
        text = event.get_message_str().strip()
        words = text.split(maxsplit=1)
        command = words[1].strip() if len(words) == 2 else "帮助"
        is_command = bool(words and words[0].lower() in {"/lingxi", "lingxi"})
        bridge = self.bridges.get(event.unified_msg_origin)
        if not is_command and bridge is None:
            return
        # Set this before awaiting network IO; never fall through to AstrBot's LLM.
        event.stop_event()

        async def reply(value):
            await event.send(event.plain_result(value))
            return True

        if is_command and command in {"身份", "id"}:
            await reply("当前私聊标识：\n" + event.unified_msg_origin
                        + "\n把它填入 AstrBot 插件配置的「允许的私聊标识」。API Token 仅在后台填写。")
            return
        if is_command and command in {"帮助", "help"}:
            await reply(HELP)
            return
        if bridge is None:
            await reply("此私聊还未被允许。先发送 /lingxi 身份，再由管理员在插件后台配置对应账号。")
            return
        try:
            if is_command:
                async with bridge.lock:
                    if command in {"绑定", "bind"}:
                        await bridge.client.bind()
                        await reply("已连接灵犀账号。现在可直接聊天、管理日程和待办；主动消息按灵犀后台设置发送。")
                    elif command in {"解绑", "unbind"}:
                        await bridge.client.unbind()
                        await reply("已停止此私聊的灵犀桥接与主动推送。")
                    elif command in {"状态", "status"}:
                        binding = await bridge.client.binding()
                        status = "已绑定灵犀。" if binding else "尚未绑定，请发送 /lingxi 绑定。"
                        if binding and binding.get("conversation_id") is None:
                            status = "原会话已删除，请发送 /lingxi 绑定 重新建立会话。"
                        await reply(status)
                    else:
                        await reply(HELP)
                return
            if any(type(component).__name__ not in {"Plain", "At", "Reply"}
                   for component in event.get_messages()):
                await reply("当前 AstrBot 桥接支持文字对话。图片、语音和文件请先在灵犀网页或原飞书渠道处理。")
                return
            if not text:
                return
            message_id = getattr(event.message_obj, "message_id", None)
            await bridge.chat(text, message_id, reply)
        except BridgeError as exc:
            messages = {
                "not_bound": "尚未绑定，请发送 /lingxi 绑定。",
                "unauthorized": "灵犀 API Token 已失效，请在插件后台更新后重新绑定。",
                "binding_mismatch": "灵犀账号已绑定其他私聊。请在后台核对配置，再明确绑定当前私聊。",
                "conversation_missing": "原会话已删除，请发送 /lingxi 绑定 重新建立会话。",
            }
            await reply(messages.get(exc.code, "灵犀暂时未完成这次请求。操作可能已经执行，请先在灵犀查看会话记录，再决定是否重新发送。"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning("灵犀桥接请求未完成（%s）", type(exc).__name__)
            await reply("灵犀桥接暂时不可用，请稍后查看会话记录。为避免重复执行任务，本条消息不会自动重发。")
