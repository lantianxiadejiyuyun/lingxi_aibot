"""Cross-channel commands, tenant isolation and in-turn model switching."""
import json
from unittest.mock import Mock, patch

from scripts.tests.support import IsolatedAppTestCase
from app.extensions import db
from app.models.conversation import Conversation, Message
from app.services.settings_service import set_setting
from app.services import model_control_service as controls
from app.utils.scoping import set_current_user_id, clear_current_user_id


class ChatControlTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        set_current_user_id(self.user.id)
        set_setting('llm_protocol', 'openai')
        set_setting('llm_base_url', 'https://api.openai.com/v1')
        set_setting('llm_model', 'gpt-5.1')
        set_setting('llm_models', ['gpt-5.1', 'gpt-5-mini'])
        set_setting('ai_persona_ack_enabled', False)
        self.conv = Conversation(user_id=self.user.id, title='controls')
        self.other_conv = Conversation(user_id=self.user.id, title='unaffected')
        db.session.add_all([self.conv, self.other_conv])
        db.session.commit()

    def _command(self, text):
        from app.ai.executor import run_chat
        events = list(run_chat(self.conv, text, self.user))
        self.assertFalse([event for event in events if event[0] == 'error'], events)
        return next(event[1] for event in events if event[0] == 'done')

    def test_commands_work_without_key_and_only_change_current_conversation(self):
        self.assertIn('/model', self._command('/'))
        self.assertIn('gpt-5-mini', self._command('/model 2'))
        self.assertIn('high', self._command('/think high'))
        state = controls.chat_controls(self.conv, self.user)
        self.assertEqual(state['model'], 'gpt-5-mini')
        self.assertEqual(state['reasoning_effort'], 'high')
        self.assertFalse(state['auto_switch'])
        self.assertEqual(controls.chat_controls(self.other_conv, self.user)['model'], 'gpt-5.1')
        self.assertIn('历史消息', self._command('/context'))

    def test_unknown_models_and_unsupported_effort_leave_valid_state(self):
        self.assertIn('不在', self._command('/model unknown'))
        self.assertIn('不支持', self._command('/think maximum'))
        self.assertEqual(controls.chat_controls(self.conv, self.user)['reasoning_effort'], 'default')
        self.assertEqual(controls.parse_model_list('gpt-5-mini\ngpt-5-mini', 'gpt-5.1'), ['gpt-5.1', 'gpt-5-mini'])
        with self.assertRaises(ValueError):
            controls.parse_model_list('some model with spaces', 'gpt-5.1')

    def test_web_commands_and_controls_api_enforce_ownership(self):
        clear_current_user_id()
        self.app.config['WTF_CSRF_ENABLED'] = False
        client = self.client_for(self.user)
        result = client.post('/chat/api/send', json={'conversation_id': self.conv.id, 'message': '/model 2'})
        self.assertEqual(result.status_code, 200)
        self.assertIn('gpt-5-mini', result.get_data(as_text=True))
        state = client.get(f'/chat/api/controls/{self.conv.id}').get_json()['data']
        self.assertEqual(state['model'], 'gpt-5-mini')
        self.assertNotIn('api_key', json.dumps(state))
        stranger = self.make_user('stranger')
        self.assertEqual(self.client_for(stranger).get(f'/chat/api/controls/{self.conv.id}').status_code, 404)
        set_current_user_id(stranger.id)
        with self.assertRaises(ValueError):
            controls.update_chat_model(self.conv, stranger, model='gpt-5.1')

    def test_feishu_commands_share_parser_and_work_without_ai_key(self):
        from app.services.feishu_inbound import _process_text

        self.user.feishu_open_id = 'ou_test_owner'
        db.session.commit()
        clear_current_user_id()
        with patch('app.services.channels.feishu_app.send_text') as send:
            _process_text(self.app, 'oc_test', '@_user_1 /model 2', 'ou_test_owner')
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[0], 'oc_test')
            self.assertIn('gpt-5-mini', send.call_args.args[1])
            _process_text(self.app, 'oc_test', '/status', 'ou_test_owner')
            self.assertIn('gpt-5-mini', send.call_args.args[1])

    def test_autonomous_switch_takes_effect_in_same_turn_without_old_tool_metadata(self):
        from app.ai.executor import run_chat
        from app.ai.llm import LLMClient

        set_setting('llm_api_key', 'isolated-not-real-key')
        requests = []

        def stream(client, messages, tools=None):
            cfg = client._read_config()
            requests.append((cfg, list(messages)))
            if len(requests) == 1:
                yield {'type': 'delta', 'text': 'UNIQUE_ASSISTANT_PLAN'}
                yield {'type': 'assistant_meta', 'data': {'reasoning_content': 'PRIVATE_REASONING', '_llm_signature': ['openai', cfg['base_url'], cfg['model'], 'default']}}
                yield {'type': 'tool_calls', 'calls': [{'id': 'call_note', 'name': 'create_note', 'arguments': json.dumps({'title': 'switch test', 'content': 'UNIQUE_GENERATED_TOOL_ARGUMENT'})}, {'id': 'call_switch', 'name': 'switch_chat_model', 'arguments': json.dumps({'model': 'gpt-5-mini', 'reason': 'simple task'})}]}
            else:
                yield {'type': 'delta', 'text': '完成'}

        with self.app.test_request_context('/'), patch.object(LLMClient, 'chat_stream', stream):
            from flask_login import login_user
            login_user(self.user)
            events = list(run_chat(self.conv, '使用适合的模型回答', self.user))
        self.assertEqual(len(requests), 2, events)
        self.assertEqual(requests[1][0]['model'], 'gpt-5-mini')
        next_messages = requests[1][1]
        self.assertFalse(any(message.get('tool_calls') or message['role'] == 'tool' for message in next_messages))
        self.assertIn('switch_chat_model', json.dumps(next_messages))
        self.assertIn('UNIQUE_ASSISTANT_PLAN', json.dumps(next_messages))
        self.assertIn('UNIQUE_GENERATED_TOOL_ARGUMENT', json.dumps(next_messages))
        self.assertNotIn('PRIVATE_REASONING', json.dumps(next_messages))
        self.assertNotIn('PRIVATE_REASONING', json.dumps(events))
        self.assertNotIn('PRIVATE_REASONING', '\n'.join(row.content for row in Message.query.all()))
        self.assertIn(('done', 'UNIQUE_ASSISTANT_PLAN\n\n完成'), events)

    def test_ai_tools_respect_manual_lock_and_limit_repeated_switches(self):
        from app.ai.tools.model_tools import switch_chat_model

        self._command('/model 2')
        with controls.bind_conversation(self.conv, self.user):
            with self.assertRaisesRegex(ValueError, '锁定'):
                switch_chat_model('change', model='gpt-5.1')
        self._command('/auto on')
        with controls.bind_conversation(self.conv, self.user):
            switch_chat_model('change', model='gpt-5.1')
            switch_chat_model('change', model='gpt-5-mini')
            with self.assertRaisesRegex(ValueError, '两次'):
                switch_chat_model('change', model='gpt-5.1')
        with self.assertRaisesRegex(ValueError, '只能在当前'):
            switch_chat_model('change', model='gpt-5.1')

    def test_compact_command_failure_preserves_chat_history(self):
        from app.ai.llm import LLMError
        for index in range(18):
            db.session.add(Message(conversation_id=self.conv.id, role='user' if index % 2 == 0 else 'assistant', content=f'fact-{index}'))
        db.session.commit()
        count = Message.query.count()
        llm = Mock(is_configured=True)
        llm.chat.side_effect = LLMError('offline')
        with patch('app.ai.llm.LLMClient', return_value=llm):
            self.assertIn('失败', self._command('/compact'))
        self.assertEqual(Message.query.count(), count + 2)
        self.assertFalse(self.conv.summary)
