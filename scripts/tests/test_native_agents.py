"""Native agents: real ledger transactions, mocked model/network boundaries."""
from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import threading
import time
from unittest.mock import patch

from scripts.tests.support import IsolatedAppTestCase, make_app
from app.extensions import db
from app.models.agent_run import AgentRun, AgentStep
from app.models.conversation import Conversation, Message
from app.services import native_agent_service as agents
from app.utils.scoping import current_user_id
from app.utils.timeutil import utcnow


def call(name, arguments=None, call_id="call-1"):
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments or {})}


class FakeLLM:
    is_configured = True
    last_assistant_meta = {}

    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def chat(self, messages, tools=None):
        self.requests.append((list(messages), tools))
        return next(self.replies)


def override(handler, **extra):
    return {"description": "测试工具", "parameters": {"type": "object", "properties": {}},
            "handler": handler, "read_only": True, **extra}


class NativeAgentTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.user_id = self.user.id

    def run_fake(self, fake, **kwargs):
        with patch.object(agents, "LLMClient", return_value=fake):
            return agents.run_agent(self.user_id, "搜索用户指定的资源", **kwargs)

    def test_multiround_search_uses_evidence_and_persists_steps(self):
        queries = []

        def search(query):
            queries.append((query, current_user_id()))
            return {"items": []} if query == "first" else {"candidate_id": 7}

        fake = FakeLLM([("先搜索", [call("search", {"query": "first"})]),
                        ("换个别名", [call("search", {"query": "second"}, "call-2")]),
                        ("找到候选资源 7", [])])
        result = self.run_fake(fake, allowed_tools=["search"], tool_overrides={"search": override(search)})
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(queries, [("first", self.user_id), ("second", self.user_id)])
        self.assertEqual(result["budget"]["rounds"], 3)
        self.assertEqual(result["budget"]["tools"], 2)
        self.assertEqual(len(result["steps"]), 5)
        self.assertIn('"candidate_id": 7', fake.requests[-1][0][-1]["content"])
        self.assertEqual(Message.query.count(), 0)

    def test_forged_tool_is_rejected_at_execution_boundary(self):
        spy = []
        fake = FakeLLM([("", [call("not_allowed")]), ("无法使用该工具", [])])
        result = self.run_fake(fake, tool_overrides={"not_allowed": override(lambda: spy.append(1))})
        self.assertEqual(spy, [])
        self.assertIn("允许列表", result["steps"][1]["result"])
        self.assertEqual(result["status"], "succeeded")

    def test_dynamic_skill_and_shell_are_not_implicit_agent_tools(self):
        for name in ("run_shell", "skill_custom_python"):
            with patch.object(agents.registry, "get_tool", return_value={
                "name": name, "fn": lambda: None, "dangerous": False,
            }):
                with self.assertRaises(ValueError):
                    self.run_fake(FakeLLM([]), allowed_tools=[name])

    def test_credentials_redacted_before_model_and_ledger(self):
        fake = FakeLLM([("", [call("search")]), ("已完成", [])])
        result = self.run_fake(fake, allowed_tools=["search"], tool_overrides={"search": override(
            lambda: {"api_key": "private-provider-key", "url": "https://name:password@example.com/file?token=private-token&x=1",
                     "text": "Authorization: Bearer abcdefg"})})
        stored = json.dumps(result)
        sent = json.dumps(fake.requests[-1][0])
        for secret in ("private-provider-key", "private-token", "abcdefg", "name:password"):
            self.assertNotIn(secret, stored)
            self.assertNotIn(secret, sent)

    def test_cross_user_reads_cancel_and_conversation_are_denied(self):
        other = self.make_user("other", False)
        other_id = other.id
        result = self.run_fake(FakeLLM([("done", [])]))
        for action in (agents.get_run, agents.cancel_agent):
            with self.assertRaises(ValueError):
                action(other_id, result["run_id"])
        conversation = Conversation(user_id=other_id, title="private")
        db.session.add(conversation)
        db.session.commit()
        with self.assertRaises(ValueError):
            self.run_fake(FakeLLM([]), conversation_id=conversation.id)
        self.assertEqual(agents.list_runs(other_id), [])

    def test_task_key_is_idempotent_including_budget_exhaustion(self):
        fake = FakeLLM([("", [call("search")])])
        result = self.run_fake(fake, max_rounds=1, task_key="media:4:search:0",
                               allowed_tools=["search"], tool_overrides={"search": override(lambda: "none")})
        self.assertEqual(result["status"], "budget_exhausted")
        again = self.run_fake(FakeLLM([]), task_key="media:4:search:0")
        self.assertEqual(again["run_id"], result["run_id"])
        self.assertEqual(again["budget"]["rounds"], 1)
        self.assertEqual(AgentRun.query.count(), 1)

    def test_cancel_after_model_prevents_tool_side_effect(self):
        fake = FakeLLM([("", [call("search")])])
        effects = []
        result = self.run_fake(fake, cancel_check=lambda: len(fake.requests) > 0,
                               allowed_tools=["search"], tool_overrides={"search": override(lambda: effects.append(1))})
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(effects, [])

    def test_pause_resumes_same_run_without_resetting_shared_budget(self):
        first_model = FakeLLM([("planning", [call("search")])])
        results = []
        paused = self.run_fake(first_model, task_key="media:pause", allowed_tools=["search"],
                               tool_overrides={"search": override(lambda: results.append(1))},
                               cancel_check=lambda: "paused" if first_model.requests else False)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(results, [])
        resumed = self.run_fake(FakeLLM([("resumed", [])]), task_key="media:pause",
                                tool_overrides={"search": override(lambda: results.append(1))})
        self.assertEqual(resumed["run_id"], paused["run_id"])
        self.assertEqual(resumed["status"], "succeeded")
        self.assertEqual(resumed["budget"]["rounds"], 2)

    def test_cancel_during_tool_cannot_become_success(self):
        holder = {}

        def search():
            agents.cancel_agent(self.user_id, holder["id"])
            return "done"

        def event(data):
            holder["id"] = data["run_id"]

        result = self.run_fake(FakeLLM([("", [call("search")])]), on_event=event,
                               allowed_tools=["search"], tool_overrides={"search": override(search)})
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["cancel_requested"])

    def test_active_lease_cannot_be_stolen(self):
        run = AgentRun(user_id=self.user_id, prompt="task", allowed_tools=[], status="running",
                       lease_owner="current-worker", lease_expires_at=utcnow() + timedelta(minutes=3))
        db.session.add(run)
        db.session.commit()
        result = self.run_fake(FakeLLM([]), run_id=run.id)
        self.assertEqual(result["status"], "running")
        db.session.refresh(run)
        self.assertEqual(run.lease_owner, "current-worker")

    def test_old_worker_does_not_overwrite_tool_step_after_lease_loss(self):
        def steal_lease():
            row = AgentRun.query.filter_by(user_id=self.user_id, status="running").one()
            row.lease_owner = "replacement-worker"
            row.checkpoint = {"phase": "replacement-checkpoint"}
            db.session.commit()
            return "old worker output must not overwrite the new checkpoint"

        result = self.run_fake(FakeLLM([("", [call("search")])]), allowed_tools=["search"],
                               tool_overrides={"search": override(steal_lease)})
        self.assertEqual(result["status"], "running")
        row = db.session.get(AgentRun, result["run_id"])
        self.assertEqual(row.lease_owner, "replacement-worker")
        self.assertEqual(row.checkpoint, {"phase": "replacement-checkpoint"})
        step = AgentStep.query.filter_by(run_id=row.id, kind="tool").one()
        self.assertEqual(step.result, "")
        self.assertEqual(step.status, "running")

    def test_heartbeat_keeps_long_model_call_from_being_claimed_twice(self):
        app = make_app(SQLALCHEMY_DATABASE_URI="sqlite:///" + str(Path(self.temp_directory.name) / "heartbeat.db"))

        def close_database():
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        self.addCleanup(close_database)
        with app.app_context():
            db.create_all()
            owner = self.make_user("heartbeat-owner").id
            queued = agents.queue_agent(owner, "Long running model", allowed_tools=[])
            started, release = threading.Event(), threading.Event()
            results = []

            class SlowLLM:
                is_configured = True
                last_assistant_meta = {}

                def __init__(self, **kwargs):
                    pass

                def chat(self, messages, tools=None):
                    started.set()
                    release.wait(timeout=5)
                    return "finished after initial lease duration", []

            def execute():
                with app.app_context():
                    results.append(agents.run_agent(owner, "", run_id=queued["run_id"]))

            with patch.object(agents, "LEASE_SECONDS", 0.3), patch.object(agents, "HEARTBEAT_INTERVAL", 0.03), \
                    patch.object(agents, "LLMClient", SlowLLM):
                thread = threading.Thread(target=execute)
                thread.start()
                try:
                    self.assertTrue(started.wait(timeout=2))
                    time.sleep(0.5)
                    competing = agents.run_agent(owner, "", run_id=queued["run_id"])
                    self.assertEqual(competing["status"], "running")
                    self.assertEqual(competing["budget"]["rounds"], 1)
                finally:
                    release.set()
                    thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results[0]["status"], "succeeded")

    def test_general_queue_is_safe_idempotent_and_worker_ignores_media_runs(self):
        queued = agents.queue_agent(self.user_id, "研究一个问题", allowed_tools=[], request_key="research-1")
        self.assertEqual(queued["status"], "queued")
        again = agents.queue_agent(self.user_id, "同一个请求", allowed_tools=[], request_key="research-1")
        self.assertEqual(again["run_id"], queued["run_id"])
        for name in ("run_shell", "download_anime", "search_anime_resources"):
            with self.assertRaises(ValueError):
                agents.queue_agent(self.user_id, "拒绝越权", allowed_tools=[name])
        untouched = AgentRun(user_id=self.user_id, prompt="media search", allowed_tools=[],
                             task_key="media:7:search:0", status="queued")
        db.session.add(untouched)
        db.session.commit()
        untouched_id = untouched.id
        with patch.object(agents, "LLMClient", return_value=FakeLLM([("研究结果", [])])):
            self.assertTrue(agents.process_queued_agent())
            self.assertFalse(agents.process_queued_agent())
        result = agents.get_run(self.user_id, queued["run_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"], "研究结果")
        self.assertEqual(db.session.get(AgentRun, untouched_id).status, "queued")

    def test_uncertain_write_is_not_replayed_on_restart(self):
        run = AgentRun(user_id=self.user_id, prompt="task", allowed_tools=["write"], status="running",
                       lease_owner="dead-worker", lease_expires_at=utcnow() - timedelta(minutes=1), rounds_used=1)
        db.session.add(run)
        db.session.commit()
        db.session.add(AgentStep(user_id=self.user_id, run_id=run.id, sequence=1,
                                 kind="tool", status="running", tool_name="write", read_only=False))
        db.session.commit()
        writes = []
        result = self.run_fake(FakeLLM([]), run_id=run.id,
                               tool_overrides={"write": override(lambda: writes.append(1), read_only=False)})
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(writes, [])

    def test_restart_reuses_completed_evidence_without_tool_replay(self):
        run = AgentRun(user_id=self.user_id, prompt="task", allowed_tools=["search"], status="running",
                       lease_owner="dead-worker", lease_expires_at=utcnow() - timedelta(minutes=1), rounds_used=2)
        db.session.add(run)
        db.session.commit()
        db.session.add(AgentStep(user_id=self.user_id, run_id=run.id, sequence=1, kind="tool",
                                 status="succeeded", tool_name="search", result="candidate 22", read_only=True))
        db.session.commit()
        fake = FakeLLM([("使用已有候选 22", [])])
        result = self.run_fake(fake, run_id=run.id, tool_overrides={"search": override(lambda: self.fail("replayed"))})
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["budget"]["rounds"], 3)
        self.assertIn("candidate 22", fake.requests[0][0][-1]["content"])
        self.assertTrue(all("tool_calls" not in message for message in fake.requests[0][0]))

    def test_children_cannot_expand_tools_or_delegate_again(self):
        parent = AgentRun(user_id=self.user_id, prompt="parent", allowed_tools=["search"], status="running")
        db.session.add(parent)
        db.session.commit()
        with self.assertRaisesRegex(ValueError, "父代理"):
            self.run_fake(FakeLLM([]), parent_run_id=parent.id, role="search", allowed_tools=["extra"],
                          tool_overrides={"extra": override(lambda: None)})
        child = AgentRun(user_id=self.user_id, prompt="child", allowed_tools=[],
                         parent_run_id=parent.id, root_run_id=parent.id, status="queued")
        db.session.add(child)
        db.session.commit()
        result = self.run_fake(FakeLLM([("", [call("delegate_agents", {"tasks": []})]), ("完成", [])]), run_id=child.id)
        self.assertEqual(result["status"], "succeeded")
        self.assertIn("允许列表", result["steps"][1]["result"])
        with self.assertRaisesRegex(ValueError, "最多一层"):
            self.run_fake(FakeLLM([]), parent_run_id=child.id, role="verify")

    def test_parallel_children_use_isolated_sessions_and_shared_budget(self):
        # SQLite's in-memory StaticPool shares one DB connection. A real file is
        # used here to exercise independent concurrent worker transactions.
        parallel_app = make_app(SQLALCHEMY_DATABASE_URI="sqlite:///" + str(Path(self.temp_directory.name) / "agents.db"))
        def close_database():
            with parallel_app.app_context():
                db.session.remove()
                db.engine.dispose()
        self.addCleanup(close_database)
        with parallel_app.app_context():
            db.create_all()
            user = self.make_user("parallel")
            user_id = user.id
            lock = threading.Lock()
            barrier = threading.Barrier(3)
            observed = []

            def probe():
                with lock:
                    observed.append((current_user_id(), id(db.session()), threading.get_ident()))
                barrier.wait(timeout=10)
                return "verified"

            class ParallelLLM:
                is_configured = True
                last_assistant_meta = {}

                def __init__(self, **kwargs):
                    self.calls = 0

                def chat(self, messages, tools=None):
                    self.calls += 1
                    if self.calls > 1:
                        return "completed", []
                    if messages[1]["content"] == "parent task":
                        tasks = [{"role": role, "prompt": role, "allowed_tools": ["probe"]}
                                 for role in ("search", "parse", "verify")]
                        return "", [call("delegate_agents", {"tasks": tasks})]
                    return "", [call("probe")]

            with patch.object(agents, "LLMClient", ParallelLLM):
                result = agents.run_agent(user_id, "parent task", max_rounds=10, allowed_tools=["probe"],
                                          tool_overrides={"probe": override(probe)})
            self.assertEqual(result["status"], "succeeded", result)
            self.assertEqual(len(observed), 3)
            self.assertEqual(len({row[1] for row in observed}), 3)
            self.assertEqual(len({row[2] for row in observed}), 3)
            self.assertEqual({row[0] for row in observed}, {user_id})
            self.assertEqual(result["budget"]["rounds"], 8)
            self.assertEqual(result["budget"]["tools"], 4)
            self.assertEqual(result["budget"]["children"], 3)
            self.assertTrue(all(child["status"] == "succeeded" for child in result["children"]))


if __name__ == "__main__":
    import unittest
    unittest.main()
