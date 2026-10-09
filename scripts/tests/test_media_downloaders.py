"""Downloader idempotency, control and status normalization with mocked RPC."""
import unittest
from unittest.mock import Mock, patch

import requests

from scripts.tests import support  # isolate application configuration
from app.services import media_downloaders as downloaders


HASH = "0123456789abcdef0123456789abcdef01234567"
CANDIDATE = {"kind": "magnet", "url": "magnet:?xt=urn:btih:" + HASH, "infohash": HASH}
HTTP = {"kind": "http", "url": "https://example.org/episode.mkv"}
KEY = "0123456789abcdef0123456789abcdef"


def response(data=None, text="", status=200):
    return Mock(status_code=status, text=text, json=Mock(return_value=data))


class MediaDownloadersTest(unittest.TestCase):
    def setUp(self):
        session = patch.object(downloaders.requests, "Session")
        self.session = session.start().return_value
        self.addCleanup(session.stop)
        safety = patch.object(downloaders, "validate_public_url", side_effect=lambda url: url)
        safety.start()
        self.addCleanup(safety.stop)

    def qb(self):
        adapter = downloaders.get_downloader({"kind": "qbittorrent", "base_url": "http://127.0.0.1:8080",
                                             "username": "test", "password": "never-return-this"})
        self.addCleanup(adapter.close)
        return adapter

    def aria(self):
        adapter = downloaders.get_downloader({"kind": "aria2", "base_url": "http://127.0.0.1:6800",
                                             "secret": "never-return-this"})
        self.addCleanup(adapter.close)
        return adapter

    def test_qb_timeout_then_reconcile_never_adds_duplicate(self):
        qb = self.qb()
        entry = {"hash": HASH, "tags": qb.external_id(KEY), "state": "downloading", "progress": 0.1}
        # login, reconcile before submit, global hash collision check, add timeout
        self.session.request.side_effect = [response(text="Ok."), response([]), response([]), requests.Timeout("secret")]
        with self.assertRaises(downloaders.DownloadError) as ctx:
            qb.submit(CANDIDATE, "/data/pending/a", KEY)
        self.assertTrue(ctx.exception.uncertain)
        self.assertTrue(ctx.exception.infrastructure)
        self.assertNotIn("secret", str(ctx.exception))
        self.session.request.reset_mock()
        self.session.request.side_effect = [response([entry])]
        result = qb.submit(CANDIDATE, "/data/pending/a", KEY)
        self.assertEqual(result["external_id"], HASH)
        self.assertEqual(self.session.request.call_count, 1)
        self.assertIn("torrents/info", self.session.request.call_args.args[1])

    def test_qb_does_not_adopt_other_tasks_torrent(self):
        qb = self.qb()
        self.session.request.side_effect = [response(text="Ok."), response([]), response([{"hash": HASH, "tags": "human"}])]
        with self.assertRaises(downloaders.DownloadError) as ctx:
            qb.submit(CANDIDATE, "/data/pending/a", KEY)
        self.assertEqual(ctx.exception.code, "duplicate_resource")
        self.assertFalse(any("torrents/add" in c.args[1] for c in self.session.request.call_args_list))

    def test_qb_queue_paused_checking_are_not_downloading(self):
        qb = self.qb()
        for raw, normalized in (("queuedDL", "queued"), ("stoppedDL", "paused"), ("pausedDL", "paused"),
                                ("checkingResumeData", "checking"), ("metaDL", "downloading"),
                                ("stalledDL", "downloading"), ("missingFiles", "failed")):
            self.assertEqual(qb._status({"hash": HASH, "state": raw})["state"], normalized)

    def test_qb_finish_preserves_files_and_4x_pause_fallback(self):
        qb = self.qb()
        self.session.request.side_effect = [response(text="Ok."), response()]
        qb.finish(HASH)
        self.assertEqual(self.session.request.call_args.kwargs["data"]["deleteFiles"], "false")
        self.session.request.side_effect = [response(status=404), response()]
        qb.pause(HASH)
        self.assertTrue(self.session.request.call_args.args[1].endswith("torrents/pause"))

    def test_qb_submission_strips_magnet_url_extras(self):
        qb = self.qb()
        entry = {"hash": HASH, "tags": qb.external_id(KEY), "state": "metaDL"}
        self.session.request.side_effect = [response(text="Ok."), response([]), response([]), response(text="Ok."), response([entry])]
        candidate = dict(CANDIDATE, url=CANDIDATE["url"] + "&xs=http://127.0.0.1/secret&tr=http://127.0.0.1/tracker")
        qb.submit(candidate, "/data/pending/a", KEY)
        add_call = self.session.request.call_args_list[3]
        self.assertEqual(add_call.kwargs["data"]["urls"], CANDIDATE["url"])

    def test_qb_replaced_hash_cannot_be_polled_or_controlled_by_old_attempt(self):
        qb = self.qb()
        qb.config["attempt_key"] = KEY
        qb._authenticated = True
        foreign = {"hash": HASH, "tags": qb.external_id("a-different-attempt"), "state": "downloading"}
        for operation in (qb.poll, qb.retry, qb.pause, qb.cancel, qb.finish):
            with self.subTest(operation=operation.__name__):
                self.session.request.reset_mock()
                self.session.request.return_value = response([foreign])
                with self.assertRaises(downloaders.DownloadError) as ctx:
                    operation(HASH)
                self.assertEqual(ctx.exception.code, "ownership_conflict")
                self.assertTrue(ctx.exception.infrastructure)
                self.assertTrue(all(call.args[0] == "GET" for call in self.session.request.call_args_list))

    def test_qb_missing_owned_download_controls_are_idempotent(self):
        qb = self.qb()
        qb.config["attempt_key"] = KEY
        qb._authenticated = True
        self.session.request.return_value = response([])
        self.assertEqual(qb.poll(HASH)["state"], "missing")
        self.assertEqual(qb.retry(HASH)["state"], "missing")
        self.assertIsNone(qb.pause(HASH))
        self.assertIsNone(qb.cancel(HASH))
        self.assertIsNone(qb.finish(HASH))
        self.assertTrue(all(call.args[0] == "GET" for call in self.session.request.call_args_list))

    def test_qb_retry_rechecks_ownership_before_reannounce_and_legacy_fallback(self):
        qb = self.qb()
        qb.config["attempt_key"] = KEY
        qb._authenticated = True
        owned = {"hash": HASH, "tags": "other-label, " + qb.external_id(KEY), "state": "pausedDL"}
        foreign = dict(owned, tags="manual-replacement")
        self.session.request.side_effect = [response([owned]), response(), response([foreign])]
        with self.assertRaises(downloaders.DownloadError) as ctx:
            qb.retry(HASH)
        self.assertEqual(ctx.exception.code, "ownership_conflict")
        self.assertEqual([c.args[1].rsplit("/", 1)[-1] for c in self.session.request.call_args_list],
                         ["info", "start", "info"])
        self.session.request.reset_mock()
        self.session.request.side_effect = [response([owned]), response(status=404), response([foreign])]
        with self.assertRaises(downloaders.DownloadError) as ctx:
            qb.pause(HASH)
        self.assertEqual(ctx.exception.code, "ownership_conflict")
        self.assertEqual([c.args[1].rsplit("/", 1)[-1] for c in self.session.request.call_args_list],
                         ["info", "stop", "info"])

    def test_qb_owned_cancel_still_preserves_files(self):
        qb = self.qb()
        qb.config["attempt_key"] = KEY
        qb._authenticated = True
        owned = {"hash": HASH, "tags": qb.external_id(KEY), "state": "downloading"}
        self.session.request.side_effect = [response([owned]), response()]
        qb.cancel(HASH)
        self.assertEqual(self.session.request.call_args.kwargs["data"], {"hashes": HASH, "deleteFiles": "false"})

    def test_aria_reserves_gid_and_reconciles_unknown_submission(self):
        aria = self.aria()
        gid = aria.external_id(KEY)
        self.session.request.side_effect = [response({"error": {"code": 1, "message": "GID not found"}}), requests.Timeout("secret")]
        with self.assertRaises(downloaders.DownloadError) as ctx:
            aria.submit(HTTP, "/data/pending/a", KEY)
        self.assertTrue(ctx.exception.uncertain)
        add = self.session.request.call_args.kwargs["json"]
        self.assertEqual(add["params"][1]["gid"], gid) if not aria.config["secret"] else self.assertEqual(add["params"][2]["gid"], gid)
        self.session.request.reset_mock()
        self.session.request.side_effect = [response({"result": {"gid": gid, "status": "active", "completedLength": "5"}})]
        status = aria.submit(HTTP, "/data/pending/a", KEY)
        self.assertEqual(status["downloaded_bytes"], 5)
        self.assertEqual(self.session.request.call_count, 1)

    def test_aria_distinguishes_storage_failure_from_expired_source(self):
        aria = self.aria()
        gid = aria.external_id(KEY)
        for code, infrastructure in (("9", True), ("16", True), ("3", False), ("22", False)):
            self.session.request.return_value = response({"result": {"status": "error", "errorCode": code,
                                                                     "errorMessage": "remote secret"}})
            state = aria.poll(gid)
            self.assertEqual(state["infrastructure"], infrastructure)
            self.assertEqual(state["state"], "failed")
            self.assertNotIn("secret", repr(state))

    def test_aria_paused_queued_checking_and_failed_retry(self):
        aria = self.aria()
        gid = aria.external_id(KEY)
        for raw, expected in (("waiting", "queued"), ("paused", "paused"), ("complete", "completed")):
            self.session.request.return_value = response({"result": {"status": raw}})
            self.assertEqual(aria.poll(gid)["state"], expected)
        self.session.request.return_value = response({"result": {"status": "active", "verifiedLength": "0"}})
        self.assertEqual(aria.poll(gid)["state"], "checking")
        self.session.request.return_value = response({"result": {"status": "error", "errorCode": "3"}})
        with self.assertRaises(downloaders.DownloadError) as ctx:
            aria.retry(gid)
        self.assertEqual(ctx.exception.code, "retry_new_attempt")

    def test_credentials_and_response_bodies_are_not_leaked(self):
        qb = self.qb()
        self.session.request.return_value = response(text="never-return-this", status=403)
        with self.assertRaises(downloaders.DownloadError) as ctx:
            qb.reconcile(KEY)
        self.assertNotIn("never-return-this", str(ctx.exception))
        self.assertEqual(ctx.exception.code, "authentication")
        aria = self.aria()
        self.session.request.return_value = response({"error": {"message": "Unauthorized never-return-this"}})
        with self.assertRaises(downloaders.DownloadError) as ctx:
            aria.poll(aria.external_id(KEY))
        self.assertNotIn("never-return-this", str(ctx.exception))

    def test_control_ids_cannot_address_every_torrent_or_escape_paths(self):
        qb = self.qb()
        with self.assertRaises(downloaders.DownloadError):
            qb.cancel("all")
        aria = self.aria()
        with self.assertRaises(downloaders.DownloadError):
            aria.poll("not-a-gid")
        with self.assertRaises(downloaders.DownloadError):
            downloaders._check_destination("../other-user")
        self.session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
