"""Long context storage and MySQL widening migration; no external database or LLM."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.tests.support import IsolatedAppTestCase
from sqlalchemy import Text, inspect
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.schema import CreateTable

from app.ai.llm import LLMClient
from app.extensions import db
from app.models.conversation import Conversation, Message
from app.services import install_service
from app.services.settings_service import set_setting
from app.utils.scoping import set_current_user_id


class MessageSchemaTests(unittest.TestCase):
    def test_mysql_uses_longtext_and_sqlite_keeps_text_with_same_contract(self):
        mysql_ddl = str(CreateTable(Message.__table__).compile(dialect=mysql.dialect()))
        sqlite_ddl = str(CreateTable(Message.__table__).compile(dialect=sqlite.dialect()))
        self.assertIn("content LONGTEXT NOT NULL", mysql_ddl)
        self.assertIn("content TEXT NOT NULL", sqlite_ddl)
        column = Message.__table__.c.content
        self.assertFalse(column.nullable)
        self.assertEqual(column.default.arg, "")
        self.assertIsNone(column.server_default)

    def _migrate(self, column, dialect=None, repeats=1, tables=None):
        engine = SimpleNamespace(dialect=dialect or mysql.dialect())
        session = Mock()
        fake_db = SimpleNamespace(engine=engine, session=session)
        inspector = Mock()
        inspector.get_table_names.return_value = ["messages"] if tables is None else tables
        inspector.get_columns.side_effect = lambda table: [column] if column else []
        statements = []

        def execute(statement):
            self.assertIn(statement.compile(dialect=engine.dialect).params, (None, {}))
            statements.append(str(statement))
            if column:
                column["type"] = mysql.LONGTEXT()

        session.execute.side_effect = execute
        messages = []
        with patch.object(install_service, "db", fake_db), \
                patch("sqlalchemy.inspect", return_value=inspector), \
                patch.object(install_service, "_migrate_multi_user"):
            for _ in range(repeats):
                install_service.ensure_schema(messages)
        return statements, messages, session

    def test_mysql_existing_text_widens_once_and_commit_is_idempotent(self):
        column = {"name": "content", "type": mysql.TEXT(), "nullable": False, "default": None}
        statements, messages, session = self._migrate(column, repeats=2)
        self.assertEqual(statements, ["ALTER TABLE messages MODIFY COLUMN content LONGTEXT NOT NULL"])
        self.assertEqual(len(messages), 1)
        self.assertIn("LONGTEXT", messages[0])
        self.assertEqual(session.commit.call_count, 2)

    def test_mysql_preserves_existing_default_collation_and_comment(self):
        column = {"name": "content", "type": mysql.TEXT(charset="utf8mb4", collation="utf8mb4_unicode_ci"),
                  "nullable": False, "default": "(':empty % kept')", "comment": "original :content 100%"}
        statements, _, _ = self._migrate(column)
        self.assertEqual(len(statements), 1)
        for fragment in ("LONGTEXT", "CHARACTER SET utf8mb4", "COLLATE utf8mb4_unicode_ci",
                         "NOT NULL", "DEFAULT (':empty % kept')", "COMMENT 'original :content 100%'"):
            self.assertIn(fragment, statements[0])

    def test_nullable_legacy_column_is_not_silently_tightened(self):
        column = {"name": "content", "type": mysql.TEXT(), "nullable": True, "default": None}
        statements, _, _ = self._migrate(column)
        self.assertEqual(statements, ["ALTER TABLE messages MODIFY COLUMN content LONGTEXT"])

    def test_existing_longtext_and_missing_table_or_column_need_no_alter(self):
        for column, tables in (
            ({"name": "content", "type": mysql.LONGTEXT(), "nullable": False}, ["messages"]),
            (None, ["messages"]), (None, []),
        ):
            with self.subTest(tables=tables, column=column):
                statements, messages, _ = self._migrate(column, tables=tables)
                self.assertEqual(statements, [])
                self.assertEqual(messages, [])

    def test_sqlite_never_receives_mysql_modify_statement(self):
        statements, messages, _ = self._migrate(
            {"name": "content", "type": Text(), "nullable": False}, dialect=sqlite.dialect())
        self.assertEqual(statements, [])
        self.assertEqual(messages, [])

    def test_failed_mysql_alter_propagates_instead_of_reporting_success(self):
        fake_db = SimpleNamespace(engine=SimpleNamespace(dialect=mysql.dialect()), session=Mock())
        fake_db.session.execute.side_effect = RuntimeError("isolated ALTER failure")
        inspector = Mock()
        inspector.get_table_names.return_value = ["messages"]
        inspector.get_columns.return_value = [{"name": "content", "type": mysql.TEXT(), "nullable": False}]
        messages = []
        with patch.object(install_service, "db", fake_db), \
                patch("sqlalchemy.inspect", return_value=inspector), \
                patch.object(install_service, "_migrate_multi_user"), \
                self.assertRaisesRegex(RuntimeError, "ALTER failure"):
            install_service.ensure_schema(messages)
        self.assertEqual(messages, [])
        fake_db.session.commit.assert_not_called()


class LongMessageStorageTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        set_current_user_id(self.user.id)
        self.conv = Conversation(user_id=self.user.id, title="long context")
        db.session.add(self.conv)
        db.session.commit()

    def test_large_user_assistant_and_tool_content_round_trip_without_truncation(self):
        content = "HEAD_FACT\n" + "长上下文数据🙂" * 12000 + "\nTAIL_FACT_应保留"
        self.assertGreater(len(content.encode("utf-8")), 65535)
        saved_ids = []
        for role in ("user", "assistant", "tool"):
            row = Message(conversation_id=self.conv.id, role=role, content=content)
            db.session.add(row)
            db.session.flush()
            saved_ids.append(row.id)
        empty = Message(conversation_id=self.conv.id, role="assistant")
        db.session.add(empty)
        db.session.commit()
        empty_id = empty.id
        db.session.expire_all()
        for row_id in saved_ids:
            self.assertEqual(db.session.get(Message, row_id).content, content)
        self.assertEqual(db.session.get(Message, empty_id).content, "")
        reflected = next(c for c in inspect(db.engine).get_columns("messages") if c["name"] == "content")
        self.assertEqual(str(reflected["type"]), "TEXT")

    def test_large_input_and_tool_result_tail_reach_actual_model_payload(self):
        from app.ai.executor import run_chat

        for key, value in {
            "llm_protocol": "openai", "llm_base_url": "https://models.example/v1",
            "llm_api_key": "isolated-storage-key", "llm_model": "long-context-model",
            "llm_context_window_tokens": 1000000, "ai_persona_ack_enabled": False,
        }.items():
            set_setting(key, value)
        user_text = "USER_HEAD\n" + "大" * 30000 + "\nUSER_TAIL_UNIQUE"
        tool_result = json.dumps({"data": "工具" * 20000, "tail": "TOOL_TAIL_UNIQUE"}, ensure_ascii=False)
        self.assertGreater(len(user_text.encode("utf-8")), 65535)
        self.assertGreater(len(tool_result.encode("utf-8")), 65535)
        sdk = Mock()
        sdk.chat.completions.create.side_effect = [
            iter([{"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "long-result", "function": {"name": "read_large_result", "arguments": "{}"},
            }]}}]}]),
            iter([{"choices": [{"delta": {"content": "已收到完整输入与工具结果尾部"}}]}]),
        ]
        tool = {"type": "function", "function": {"name": "read_large_result", "parameters": {"type": "object"}}}
        with patch.object(LLMClient, "_build_openai", return_value=sdk), \
                patch("app.utils.urlsafety.check_provider_url", side_effect=lambda value: value), \
                patch("app.ai.executor.registry.openai_tools", return_value=[tool]), \
                patch("app.ai.executor.registry.execute_tool", return_value=tool_result):
            events = list(run_chat(self.conv, user_text, self.user))
        self.assertFalse([payload for kind, payload in events if kind == "error"], events)
        self.assertTrue(any(kind == "done" for kind, _ in events))
        self.assertEqual(sdk.chat.completions.create.call_count, 2)
        for call in sdk.chat.completions.create.call_args_list:
            sent_users = [m["content"] for m in call.kwargs["messages"] if m["role"] == "user"]
            self.assertIn(user_text, sent_users)
        final_request = sdk.chat.completions.create.call_args.kwargs["messages"]
        self.assertIn(tool_result, [m["content"] for m in final_request if m["role"] == "tool"])
        saved_user = Message.query.filter_by(conversation_id=self.conv.id, role="user").one()
        saved_tool = Message.query.filter_by(conversation_id=self.conv.id, role="tool").one()
        self.assertEqual(saved_user.content, user_text)
        self.assertEqual(saved_tool.content, tool_result)

    def test_init_db_cli_invokes_schema_migration_for_existing_database(self):
        with patch.object(install_service, "ensure_schema", wraps=install_service.ensure_schema) as migration:
            result = self.app.test_cli_runner().invoke(args=["init-db"])
        self.assertEqual(result.exit_code, 0, result.output)
        migration.assert_called_once()
        self.assertIn("初始化完成", result.output)
        self.assertEqual(self.user.id, db.session.get(type(self.user), self.user.id).id)


if __name__ == "__main__":
    unittest.main()
