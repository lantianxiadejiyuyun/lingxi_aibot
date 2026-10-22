"""Rust bridge compatibility and ACK-only recovery; all endpoints are mocked."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from integrations.astrbot.bridge import Account, Bridge, BridgeError, Client, parse_accounts
from scripts.tests.test_astrbot_plugin import Store, queue_item

ACCOUNT = Account('v2', 'http://lingxi.test/api/v2', 'qa-token', 'qq:FriendMessage:123')
BINDING = {'bound': True, 'session': ACCOUNT.session, 'conversation_id': 'uuid-conversation'}

class RustBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_accepts_v2_base(self):
        result = parse_accounts({'accounts': [{'api_base': ACCOUNT.api_base,
            'api_token': ACCOUNT.api_token, 'private_session': ACCOUNT.session}]})
        self.assertEqual(result[0].api_base, ACCOUNT.api_base)

    async def test_accepts_string_conversation_id(self):
        http = SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(status_code=200,
            json=lambda: {'ok': True, 'data': BINDING})))
        self.assertEqual((await Client(ACCOUNT, http).binding())['conversation_id'], 'uuid-conversation')

    async def test_chat_uses_bound_v2_endpoint_and_a_request_id(self):
        store = Store()
        client = SimpleNamespace(account=ACCOUNT, binding=AsyncMock(return_value=BINDING),
            request=AsyncMock(return_value={'reply': 'hello'}))
        bridge = Bridge(client, store.load, store.save)
        await bridge.chat('你好', None, AsyncMock(return_value=True))
        method, path, body = client.request.call_args.args
        self.assertEqual((method, path), ('POST', '/integrations/astrbot/chat'))
        self.assertEqual(body['conversation_id'], 'uuid-conversation')
        self.assertTrue(body['request_id'].startswith('astrbot-'))

    async def test_lost_ack_retries_only_ack_without_resending(self):
        store = Store()
        failed = False
        claimed = False
        async def request(method, path, body):
            nonlocal failed, claimed
            if path.endswith('/claim'):
                if claimed:
                    return []
                claimed = True
                return [queue_item(session=ACCOUNT.session)]
            if not failed:
                failed = True
                raise BridgeError('network')
            return {'status': 'sent'}
        client = SimpleNamespace(account=ACCOUNT, binding=AsyncMock(return_value=BINDING), request=AsyncMock(side_effect=request))
        bridge = Bridge(client, store.load, store.save)
        send = AsyncMock(return_value=True)
        with self.assertRaises(BridgeError):
            await bridge.poll(send)
        self.assertIn('1', store.value['acks'])
        await bridge.poll(send)
        self.assertEqual(send.await_count, 1)
        self.assertEqual(store.value['acks'], {})
