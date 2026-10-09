"""Download search uses the real native agent and persistent candidate ledger."""
from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import patch

from scripts.tests.support import IsolatedAppTestCase, make_app
from app.extensions import db
from app.models.agent_run import AgentRun
from app.models.conversation import Message
from app.models.media import DownloadAttempt, DownloadTask, ResourceCandidate
from app.services import media_service, native_agent_service
from app.services.storage_service import create_location
from app.utils.timeutil import utcnow


def tool(name, args=None):
    return {"id": name, "name": name, "arguments": json.dumps(args or {})}


def candidate(number):
    return {"title": f"Synthetic animation episode {number}", "kind": "http",
            "fingerprint": f"http:synthetic-{number}", "url": f"https://example.com/test-{number}.mp4",
            "source_url": "https://example.com/", "size": 100}


class MediaAgentIntegrationTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        # Independent connections are required by real concurrent child runs.
        self.media_app = make_app(SQLALCHEMY_DATABASE_URI="sqlite:///" + str(Path(self.temp_directory.name) / "media-agents.db"))
        self.media_context = self.media_app.app_context()
        self.media_context.push()
        self.addCleanup(self.media_context.pop)

        def close_database():
            db.session.remove()
            db.engine.dispose()
        self.addCleanup(close_database)
        db.create_all()
        self.user = self.make_user()
        self.user_id = self.user.id
        root = Path(self.temp_directory.name) / "storage"
        root.mkdir()
        location = create_location(self.user_id, {"name": "Test disk", "root_path": str(root), "min_free_bytes": 0})
        task = media_service.create_task(self.user_id, {"storage_id": location.id, "title": "Synthetic animation"})
        task.lease_token = "integration-worker"
        task.lease_until = utcnow() + timedelta(minutes=3)
        db.session.commit()
        self.task_id = task.id
        for name, value in (("read_config", {"sources": []}),
                            ("downloader_config", {"kind": "aria2", "endpoint": "http://localhost:6800"})):
            mock = patch.object(media_service, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def run_search(self, fake, side_effect):
        with patch.object(native_agent_service, "LLMClient", fake), \
                patch("app.services.media_sources.search_resources", side_effect=side_effect):
            media_service._search(self.task_id, "integration-worker")
        db.session.rollback()
        return db.session.get(DownloadTask, self.task_id)

    def test_failed_download_launches_new_agent_and_selects_new_resource(self):
        seen = []
        prompts = []

        class SearchLLM:
            is_configured = True
            last_assistant_meta = {}

            def __init__(self, **kwargs):
                self.round = 0

            def chat(self, messages, tools=None):
                self.round += 1
                if self.round == 1:
                    prompts.append(messages[1]["content"])
                    return "", [tool("search_anime_resources", {"query": "different alias"})]
                if self.round == 2:
                    selected_id = json.loads(messages[-1]["content"])[0]["id"]
                    return "", [tool("choose_media_candidate", {"candidate_id": selected_id})]
                return "已核对并选定可下载资源", []

        def search(criteria, sources=None, exclude_fingerprints=None):
            seen.append(list(exclude_fingerprints or []))
            return [candidate(len(seen))]

        task = self.run_search(SearchLLM, search)
        self.assertEqual(task.state, "submitting")
        first_run = task.agent_run_id
        first_attempt = db.session.get(DownloadAttempt, task.current_attempt_id)
        first_candidate = db.session.get(ResourceCandidate, first_attempt.candidate_id)
        first_candidate_id = first_candidate.id
        media_service._recover(task, first_attempt, "Synthetic download exhausted retry budget")
        db.session.commit()
        task = self.run_search(SearchLLM, search)
        self.assertEqual(task.state, "submitting")
        self.assertNotEqual(task.agent_run_id, first_run)
        self.assertEqual(task.recovery_count, 1)
        new_attempt = db.session.get(DownloadAttempt, task.current_attempt_id)
        self.assertNotEqual(new_attempt.candidate_id, first_candidate_id)
        self.assertEqual(DownloadAttempt.query.filter_by(task_id=self.task_id).count(), 2)
        self.assertEqual(seen, [[], ["http:synthetic-1"]])
        self.assertIn("Synthetic download exhausted retry budget", prompts[1])
        self.assertIn("http:synthetic-1", prompts[1])
        self.assertEqual(AgentRun.query.count(), 2)
        self.assertEqual(Message.query.filter_by(conversation_id=task.conversation_id, role="assistant").count(), 2)

    def test_real_subagent_searches_but_cannot_choose(self):
        selected_ids = []
        schemas_seen = []

        class DelegatingLLM:
            is_configured = True
            last_assistant_meta = {}

            def __init__(self, **kwargs):
                self.round = 0

            def chat(self, messages, tools=None):
                self.round += 1
                is_child = messages[1]["content"].startswith("child ")
                names = [entry["function"]["name"] for entry in tools or []]
                schemas_seen.append((is_child, names))
                if is_child:
                    if self.round == 1:
                        return "", [tool("search_anime_resources", {"query": messages[1]["content"]})]
                    if self.round == 2:
                        item = json.loads(messages[-1]["content"])[0]
                        selected_ids.append(item["id"])
                        # A malicious/model-mistaken child tries a write it was
                        # never granted. Runtime enforcement must reject it.
                        return "", [tool("choose_media_candidate", {"candidate_id": item["id"]})]
                    return "已找到候选", []
                if self.round == 1:
                    tasks = [{"role": "search", "prompt": "child alias", "allowed_tools": ["search_anime_resources"]}]
                    return "", [tool("delegate_agents", {"tasks": tasks})]
                if self.round == 2:
                    return "", [tool("choose_media_candidate", {"candidate_id": selected_ids[0]})]
                return "已核验子代理发现的资源", []

        task = self.run_search(DelegatingLLM, lambda *args, **kwargs: [candidate(1)])
        self.assertEqual(task.state, "submitting")
        run = native_agent_service.get_run(self.user_id, task.agent_run_id)
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(len(run["children"]), 1)
        child = native_agent_service.get_run(self.user_id, run["children"][0]["run_id"])
        self.assertEqual(child["steps"][3]["status"], "failed")
        self.assertIn("允许列表", child["steps"][3]["result"])
        for is_child, names in schemas_seen:
            if is_child:
                self.assertNotIn("choose_media_candidate", names)
                self.assertNotIn("delegate_agents", names)

    def test_cancellation_after_selection_never_creates_download_attempt(self):
        owner = self.user_id

        class CancellingLLM:
            is_configured = True
            last_assistant_meta = {}

            def __init__(self, **kwargs):
                self.round = 0

            def chat(self, messages, tools=None):
                self.round += 1
                if self.round == 1:
                    return "", [tool("search_anime_resources")]
                if self.round == 2:
                    resource_id = json.loads(messages[-1]["content"])[0]["id"]
                    return "", [tool("choose_media_candidate", {"candidate_id": resource_id})]
                active = AgentRun.query.filter_by(user_id=owner, status="running").first()
                native_agent_service.cancel_agent(owner, active.id)
                return "This response arrived after cancellation", []

        task = self.run_search(CancellingLLM, lambda *args, **kwargs: [candidate(1)])
        self.assertEqual(task.state, "needs_input")
        self.assertEqual(DownloadAttempt.query.filter_by(task_id=self.task_id).count(), 0)
        self.assertEqual(native_agent_service.get_run(owner, task.agent_run_id)["status"], "cancelled")

    def test_paused_search_resumes_same_agent_with_evidence_and_budget(self):
        task_id = self.task_id

        class PausingLLM:
            is_configured = True
            last_assistant_meta = {}

            def __init__(self, **kwargs):
                self.round = 0

            def chat(self, messages, tools=None):
                self.round += 1
                if self.round == 1:
                    return "", [tool("search_anime_resources")]
                resource_id = json.loads(messages[-1]["content"])[0]["id"]
                task = db.session.get(DownloadTask, task_id)
                task.control = "pause"
                db.session.commit()
                return "", [tool("choose_media_candidate", {"candidate_id": resource_id})]

        paused_task = self.run_search(PausingLLM, lambda *args, **kwargs: [candidate(1)])
        run_id = paused_task.agent_run_id
        paused = native_agent_service.get_run(self.user_id, run_id)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["budget"]["rounds"], 2)
        self.assertEqual(DownloadAttempt.query.count(), 0)
        media_service._handle_control(paused_task)
        media_service.action(self.user_id, task_id, "resume")
        media_service._handle_control(db.session.get(DownloadTask, task_id))
        evidence = []

        class ResumingLLM:
            is_configured = True
            last_assistant_meta = {}

            def __init__(self, **kwargs):
                self.round = 0

            def chat(self, messages, tools=None):
                self.round += 1
                if self.round == 1:
                    evidence.append(messages[-1]["content"])
                    resource_id = ResourceCandidate.query.filter_by(task_id=task_id).first().id
                    return "", [tool("choose_media_candidate", {"candidate_id": resource_id})]
                return "使用暂停前已检索的资源继续下载", []

        task = self.run_search(ResumingLLM, lambda *args, **kwargs: self.fail("Completed search was unnecessarily replayed"))
        self.assertEqual(task.state, "submitting")
        self.assertEqual(task.agent_run_id, run_id)
        resumed = native_agent_service.get_run(self.user_id, run_id)
        self.assertEqual(resumed["status"], "succeeded")
        self.assertEqual(resumed["budget"]["rounds"], 4)
        self.assertIn("http:synthetic-1", evidence[0])
        self.assertEqual(AgentRun.query.count(), 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
