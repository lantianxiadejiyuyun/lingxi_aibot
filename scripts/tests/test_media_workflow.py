"""Whole download lifecycle, retries and control races without external services."""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.tests.support import IsolatedAppTestCase
from app.extensions import db
from app.models.media import DownloadTask, DownloadAttempt, ResourceCandidate, MediaEvent
from app.services import media_service as service
from app.services.media_config import save_config, read_config, MediaError
from app.services.media_downloaders import DownloadError
from app.services.storage_service import create_location
from app.utils.timeutil import utcnow


class MediaWorkflowTest(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.uid = self.user.id
        self.root = Path(self.temp_directory.name) / "library"
        self.root.mkdir()
        self.location = create_location(self.uid, {"name": "Test", "kind": "local", "root_path": str(self.root), "download_path": str(self.root), "min_free_bytes": 0})
        save_config(self.uid, {"downloaders": [{"kind": "aria2", "base_url": "http://localhost:6800", "secret": "sensitive-key"}], "sources": []})
        self.adapter = Mock()
        self.adapter.reconcile.return_value = None
        self.adapter.submit.return_value = self.status()
        self.adapter.poll.return_value = self.status()
        p = patch("app.services.media_downloaders.get_downloader", return_value=self.adapter)
        p.start()
        self.addCleanup(p.stop)

    def status(self, state="downloading", downloaded=10, total=100):
        return {"external_id": "f" * 16, "state": state, "downloaded_bytes": downloaded, "total_bytes": total, "speed": 10}

    def task(self, **extra):
        return service.create_task(self.uid, {"title": "Synthetic show", "storage_id": self.location.id, **extra})

    def ready(self, task):
        candidate = ResourceCandidate(user_id=self.uid, task_id=task.id, fingerprint="url:synthetic", data={"title": "Synthetic show", "url": "https://example.org/a.mkv", "kind": "http", "fingerprint": "url:synthetic"})
        db.session.add(candidate)
        db.session.flush()
        service._new_attempt(task, candidate)
        db.session.commit()
        return db.session.get(DownloadAttempt, task.current_attempt_id)

    def tick(self, task):
        task.next_run_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        self.assertTrue(service.process_one())
        db.session.expire_all()
        return db.session.get(DownloadTask, task.id)

    def test_same_request_is_idempotent_and_scoped(self):
        first = self.task(request_key="request-1")
        self.assertEqual(first.id, self.task(request_key="request-1").id)
        other = self.make_user("other")
        with self.assertRaises(MediaError):
            service.get_task(other.id, first.id)
        self.assertEqual([], service.events(other.id))

    def test_submit_then_complete_archives_actual_file(self):
        task = self.task()
        attempt = self.ready(task)
        task = self.tick(task)
        self.assertEqual("downloading", task.state)
        pending = Path(attempt.paths["local_path"])
        (pending / "episode.mkv").write_bytes(b"\x1aE\xdf\xa3" + b"synthetic video")
        self.adapter.poll.return_value = self.status("completed", 100)
        self.adapter.poll.return_value["files"] = [{"path": str(pending / "episode.mkv"), "size": 19}]
        task = self.tick(task)
        self.assertEqual("finalizing", task.state)
        task = self.tick(task)
        self.assertEqual("completed", task.state)
        self.adapter.finish.assert_called_once()
        self.assertTrue(any(self.root.glob("completed/**/episode.mkv")))
        self.assertTrue(MediaEvent.query.filter_by(task_id=task.id, kind="completed", notify_pending=True).first())

    def test_timeout_reconciles_same_submission_key_after_restart(self):
        task = self.task()
        attempt = self.ready(task)
        key = attempt.attempt_key
        self.adapter.submit.side_effect = DownloadError("timeout", infrastructure=True, uncertain=True)
        task = self.tick(task)
        self.assertEqual("waiting_infrastructure", task.state)
        self.adapter.reconcile.return_value = self.status()
        task = self.tick(task)
        self.assertEqual("downloading", task.state)
        self.assertEqual(key, db.session.get(DownloadAttempt, attempt.id).attempt_key)
        self.adapter.submit.assert_called_once()
        self.assertEqual(0, task.recovery_count)

    def test_stall_retries_then_triggers_fresh_ai_search(self):
        task = self.task()
        attempt = self.ready(task)
        task = self.tick(task)
        attempt.last_progress_at = utcnow() - timedelta(hours=1)
        attempt.retry_count = 2
        db.session.commit()
        task = self.tick(task)
        self.assertEqual("searching", task.state)
        self.assertEqual(1, task.recovery_count)
        self.assertTrue(db.session.get(ResourceCandidate, attempt.candidate_id).rejected)
        self.adapter.cancel.assert_called_once()
        with patch.object(service, "_search") as search:
            self.tick(task)
            search.assert_called_once()

    def test_queued_paused_checking_never_consume_resource_budget(self):
        task = self.task()
        attempt = self.ready(task)
        self.tick(task)
        for state in ("queued", "paused", "checking"):
            attempt.last_progress_at = utcnow() - timedelta(hours=2)
            self.adapter.poll.return_value = self.status(state)
            db.session.commit()
            task = self.tick(task)
            self.assertEqual("downloading", task.state)
            self.assertEqual(0, attempt.retry_count)
        self.adapter.cancel.assert_not_called()

    def test_missing_nas_marker_waits_without_ai_search(self):
        task = self.task()
        self.ready(task)
        (self.root / self.location.marker_name).unlink()
        with patch.object(service, "_search") as search:
            task = self.tick(task)
        self.assertEqual("waiting_infrastructure", task.state)
        self.assertEqual(0, task.recovery_count)
        search.assert_not_called()
        self.adapter.submit.assert_not_called()

    def test_cancel_uncertain_submit_reconciles_before_stopping(self):
        task = self.task()
        self.ready(task)
        self.adapter.reconcile.return_value = self.status()
        service.action(self.uid, task.id, "cancel")
        task = self.tick(task)
        self.assertEqual("cancelled", task.state)
        self.adapter.cancel.assert_called_once_with("f" * 16)
        self.adapter.submit.assert_not_called()

    def test_cancel_arriving_during_remote_submit_is_not_lost(self):
        task = self.task()
        self.ready(task)
        def submit(*_):
            service.action(self.uid, task.id, "cancel")
            return self.status()
        self.adapter.submit.side_effect = submit
        task = self.tick(task)
        self.assertEqual("cancel", task.control)
        self.assertNotEqual("completed", task.state)
        task = self.tick(task)
        self.assertEqual("cancelled", task.state)

    def test_resource_budget_is_persistent(self):
        task = self.task(policy={"max_recoveries": 0})
        attempt = self.ready(task)
        self.tick(task)
        self.adapter.poll.return_value = self.status("missing")
        task = self.tick(task)
        self.assertEqual("failed", task.state)
        with self.assertRaises(MediaError):
            service.action(self.uid, task.id, "retry")

    def test_aria_failed_retry_uses_new_key_same_partial_directory(self):
        task = self.task()
        attempt = self.ready(task)
        self.tick(task)
        task.state = "retrying"
        attempt.retry_count = 1
        key, paths = attempt.attempt_key, dict(attempt.paths)
        db.session.commit()
        self.adapter.retry.side_effect = DownloadError("new gid", code="retry_new_attempt")
        task = self.tick(task)
        self.assertEqual("submitting", task.state)
        self.assertNotEqual(key, attempt.attempt_key)
        self.assertEqual(paths, attempt.paths)
        self.assertEqual(1, attempt.retry_count)
        self.assertEqual(0, task.recovery_count)

    def test_credentials_are_encrypted_and_not_returned(self):
        from app.models.setting import Setting
        self.assertNotIn("sensitive-key", str(Setting.query.filter_by(key="media_config").first().value))
        self.assertNotIn("sensitive-key", str(read_config(self.uid)))
        self.assertEqual("sensitive-key", read_config(self.uid, secrets=True)["downloaders"][0]["secret"])

    def test_live_lease_cannot_be_claimed_twice(self):
        task = self.task()
        first = service._claim()
        self.assertEqual(task.id, first[0])
        self.assertIsNone(service._claim())
        task.lease_until = utcnow() - timedelta(seconds=1)
        db.session.commit()
        second = service._claim()
        self.assertEqual(task.id, second[0])
        self.assertNotEqual(first[1], second[1])

    def test_fake_video_error_page_triggers_new_search(self):
        task = self.task()
        attempt = self.ready(task)
        self.tick(task)
        file = Path(attempt.paths["local_path"]) / "episode.mkv"
        file.write_bytes(b"<html>login required</html>")
        self.adapter.poll.return_value = {**self.status("completed", 100), "files": [{"path": str(file), "size": file.stat().st_size}]}
        task = self.tick(task)
        self.assertEqual("searching", task.state)
        self.assertIn("登录", task.error)
        self.assertEqual(1, task.recovery_count)
        self.assertFalse((self.root / "completed").exists())

    def test_storage_outage_pauses_remote_then_resumes_without_budget(self):
        from app.services.storage_service import StorageError
        task = self.task()
        attempt = self.ready(task)
        self.tick(task)
        with patch.object(service, "_healthy", side_effect=StorageError("NAS offline", "storage_offline", 503)):
            task = self.tick(task)
        self.adapter.pause.assert_called_once()
        self.assertEqual("waiting_infrastructure", task.state)
        self.assertTrue(attempt.infrastructure_paused)
        task = self.tick(task)
        self.adapter.retry.assert_called_once()
        self.assertEqual("downloading", task.state)
        self.assertFalse(attempt.infrastructure_paused)
        self.assertEqual(0, task.recovery_count)

    def test_pause_during_archive_then_resume_does_not_redownload(self):
        from app.services.storage_service import finalize_attempt
        task = self.task()
        attempt = self.ready(task)
        self.tick(task)
        file = Path(attempt.paths["local_path"]) / "episode.mkv"
        file.write_bytes(b"\x1aE\xdf\xa3test")
        self.adapter.poll.return_value = {**self.status("completed", 100), "files": [{"path": str(file), "size": 8}]}
        task = self.tick(task)
        def archive_then_pause(*args, **kwargs):
            result = finalize_attempt(*args, **kwargs)
            service.action(self.uid, task.id, "pause")
            return result
        with patch("app.services.storage_service.finalize_attempt", side_effect=archive_then_pause):
            task = self.tick(task)
        self.assertEqual("finalizing", task.state)
        self.assertEqual("pause", task.control)
        self.assertIsNone(MediaEvent.query.filter_by(task_id=task.id, kind="completed").first())
        task = self.tick(task)
        self.assertEqual("paused", task.state)
        service.action(self.uid, task.id, "resume")
        task = self.tick(task)
        self.assertEqual("finalizing", task.state)
        task = self.tick(task)
        self.assertEqual("completed", task.state)
        self.adapter.retry.assert_not_called()
        self.assertEqual(1, self.adapter.submit.call_count)

    def test_notifications_retry_only_failed_channels(self):
        task = self.task()
        event = service._event(task, "failed", {"message": "synthetic failure"}, notify=True)
        db.session.commit()
        with patch("app.services.notify_service.default_channels", return_value=["inapp", "feishu_app"]), \
             patch("app.services.notify_service.notify") as notify:
            notify.return_value = [SimpleNamespace(channel="inapp", status="sent"), SimpleNamespace(channel="feishu_app", status="failed")]
            service.deliver_notifications()
            self.assertTrue(event.notify_pending)
            self.assertEqual(["feishu_app"], event.notify_channels)
            event.notify_lease_until = utcnow() - timedelta(seconds=1)
            db.session.commit()
            notify.return_value = [SimpleNamespace(channel="feishu_app", status="sent")]
            service.deliver_notifications()
            self.assertEqual(["feishu_app"], notify.call_args.kwargs["channels"])
            self.assertEqual(self.uid, notify.call_args.kwargs["user_id"])
            self.assertFalse(event.notify_pending)

    def test_admin_can_assign_downloaders_without_erasing_other_kind(self):
        member = self.make_user("member", is_admin=False)
        save_config(self.uid, {"owner_id": member.id, "downloaders": [{"kind": "aria2", "base_url": "http://localhost:6800", "secret": "other-secret"}]})
        save_config(self.uid, {"owner_id": member.id, "downloaders": [{"kind": "qbittorrent", "base_url": "http://localhost:8080", "password": "other-password"}]})
        self.assertEqual(2, len(read_config(member.id)["downloaders"]))
        self.assertEqual("other-secret", read_config(member.id, secrets=True)["downloaders"][0]["secret"])
        self.assertEqual("sensitive-key", read_config(self.uid, secrets=True)["downloaders"][0]["secret"])
        with self.assertRaises(MediaError):
            save_config(member.id, {"owner_id": self.uid})
