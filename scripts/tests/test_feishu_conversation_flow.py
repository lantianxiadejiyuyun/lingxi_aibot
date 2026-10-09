"""Offline regression coverage for Feishu ordering and complete replies."""
import json
import threading
import unittest
from unittest.mock import Mock, patch

from scripts.tests.support import IsolatedAppTestCase
from app.extensions import db
from app.services import feishu_inbound as inbound
from app.services.channels import feishu_app
from app.utils.scoping import current_user_id


class FeishuQueueTests(unittest.TestCase):
    def setUp(self):
        self.app = object()
        self.workers = []
        inbound._seen.clear()
        inbound._pending.clear()
        inbound._pending_ids.clear()
        patcher = patch.object(inbound.threading, "Thread", side_effect=self.thread)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        inbound._seen.clear()
        inbound._pending.clear()
        inbound._pending_ids.clear()

    def thread(self, *, target, args, daemon):
        worker = Mock()
        worker.start.side_effect = lambda: self.workers.append((target, args))
        return worker

    def message(self, message_id, *, sender="sender", kind="text"):
        return {"message_id": message_id, "chat_id": "chat", "open_id": sender,
                "type": kind, "text": message_id, "image_key": message_id}

    def drain(self, index):
        target, args = self.workers[index]
        target(*args)

    def test_text_and_images_share_fifo_but_other_sender_gets_own_worker(self):
        for msg in (self.message("first"), self.message("second", kind="image"),
                    self.message("third"), self.message("other", sender="other")):
            inbound.dispatch(self.app, msg)
        self.assertEqual(len(self.workers), 2)
        observed = []
        with patch.object(inbound, "_process_text", side_effect=lambda app, chat, text, sender:
                          observed.append((sender, text))), \
                patch.object(inbound, "_process_image", side_effect=lambda app, chat, mid, key, sender:
                             observed.append((sender, mid))):
            self.drain(1)
            self.drain(0)
        self.assertEqual(observed, [("other", "other"), ("sender", "first"),
                                    ("sender", "second"), ("sender", "third")])
        self.assertFalse(inbound._pending)
        self.assertFalse(inbound._pending_ids)

    def test_retry_during_backlog_is_deduplicated_even_after_recent_cache_eviction(self):
        first = self.message("first")
        inbound.dispatch(self.app, first)

        def process(app, chat, text, sender):
            for number in range(250):
                inbound.mark_seen(f"unrelated-{number}")
            inbound.dispatch(self.app, first)

        with patch.object(inbound, "_process_text", side_effect=process) as handle:
            self.drain(0)
        self.assertEqual(handle.call_count, 1)
        inbound.dispatch(self.app, first)
        self.assertEqual(len(self.workers), 1)

    def test_failure_does_not_strand_later_messages(self):
        inbound.dispatch(self.app, self.message("first"))
        inbound.dispatch(self.app, self.message("second"))
        with patch.object(inbound, "_process_text", side_effect=[RuntimeError("failed"), None]) as handle, \
                self.assertLogs(inbound.logger, level="ERROR"):
            self.drain(0)
        self.assertEqual([call.args[2] for call in handle.call_args_list], ["first", "second"])
        self.assertFalse(inbound._pending)
        self.assertFalse(inbound._pending_ids)

    def test_launch_failure_allows_delivery_retry(self):
        msg = self.message("first")
        with patch.object(inbound.threading, "Thread", side_effect=RuntimeError("cannot start")):
            with self.assertRaises(RuntimeError):
                inbound.dispatch(self.app, msg)
        inbound.dispatch(self.app, msg)
        self.assertEqual(len(self.workers), 1)

    def test_invalid_message_does_not_consume_message_id(self):
        msg = self.message("first")
        inbound.dispatch(self.app, dict(msg, text=""))
        inbound.dispatch(self.app, msg)
        self.assertEqual(len(self.workers), 1)


class FeishuReplyTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("feishu-flow")
        self.user.feishu_open_id = "ou_flow"
        db.session.commit()

    def process(self, events, send_effect=None):
        with patch("app.ai.executor.run_chat", return_value=iter(events)), \
                patch.object(feishu_app, "send_text", side_effect=send_effect) as send, \
                patch("app.utils.netinfo.feishu_sdk_page_warning", return_value=""):
            inbound._process_text(self.app, "oc_flow", "hello", self.user.feishu_open_id)
        return send

    def test_successful_ack_is_sent_once_with_complete_final_body(self):
        send = self.process([("ack", "收到：hello"), ("delta", "计划"),
                             ("delta", "\n\n结果"), ("done", "收到：hello\n\n计划\n\n结果")])
        self.assertEqual([call.args[1] for call in send.call_args_list],
                         ["收到：hello", "计划\n\n结果"])

    def test_ack_and_notice_delivery_failures_do_not_abort_final_reply(self):
        send = self.process([("ack", "收到：hello"), ("notice", "切换模型"),
                             ("done", "收到：hello\n\n完整结果")],
                            [RuntimeError("progress failed"), RuntimeError("notice failed"), None])
        self.assertEqual(send.call_count, 3)
        self.assertEqual(send.call_args.args[1], "收到：hello\n\n完整结果")

    def test_partial_reply_is_preserved_and_interruption_is_explained(self):
        send = self.process([("ack", "收到：hello"), ("delta", "已有结果"),
                             ("delta", "，还没完成"), ("error", "模型连接中断")])
        self.assertEqual(send.call_count, 2)
        self.assertIn("已有结果，还没完成", send.call_args.args[1])
        self.assertIn("本次回复未完成：模型连接中断", send.call_args.args[1])

    def test_unexpected_exception_gives_visible_failure_instead_of_silence(self):
        scopes = []
        with patch("app.ai.executor.run_chat", side_effect=RuntimeError("private-debug-details")), \
                patch.object(feishu_app, "send_text", side_effect=lambda *args:
                             scopes.append(current_user_id())) as send, \
                self.assertLogs(inbound.logger, level="ERROR"):
            inbound._process_text(self.app, "oc_flow", "hello", self.user.feishu_open_id)
        self.assertEqual(send.call_count, 1)
        self.assertIn("失败", send.call_args.args[1])
        self.assertNotIn("private-debug-details", send.call_args.args[1])
        self.assertEqual(scopes, [self.user.id])
        self.assertEqual(current_user_id(), 0)

    def test_empty_response_does_not_claim_task_was_completed(self):
        send = self.process([("done", "")])
        self.assertIn("未能生成有效回复", send.call_args.args[1])

    def test_normal_reply_does_not_wait_for_page_dns_diagnostics(self):
        with patch("app.ai.executor.run_chat", return_value=iter([("done", "你好，今天过得怎么样？")])), \
                patch.object(feishu_app, "send_text") as send, \
                patch("app.utils.netinfo.feishu_sdk_page_warning") as diagnose:
            inbound._process_text(self.app, "oc_flow", "hello", self.user.feishu_open_id)
        diagnose.assert_not_called()
        self.assertEqual(send.call_args.args[1], "你好，今天过得怎么样？")

    def test_page_reply_keeps_reachability_warning(self):
        with patch("app.ai.executor.run_chat", return_value=iter([("done", "访问地址：https://example.invalid/p/1")])), \
                patch.object(feishu_app, "send_text") as send, \
                patch("app.utils.netinfo.feishu_sdk_page_warning", return_value="请配置公网地址") as diagnose:
            inbound._process_text(self.app, "oc_flow", "hello", self.user.feishu_open_id)
        diagnose.assert_called_once()
        self.assertIn("⚠️ 请配置公网地址", send.call_args.args[1])


class FeishuTextDeliveryTests(unittest.TestCase):
    def test_long_multilingual_reply_is_delivered_without_truncation(self):
        original = "第一部分。" * 650 + "\n\n" + "🙂第二部分。" * 900 + "\n最后一行"
        response = Mock()
        response.json.return_value = {"code": 0}
        with patch.object(feishu_app, "get_tenant_access_token", return_value="offline-token") as token, \
                patch.object(feishu_app.requests, "post", return_value=response) as post:
            feishu_app.send_text("ou_receiver", original, receive_id_type="open_id")
        chunks = [json.loads(call.kwargs["json"]["content"])["text"] for call in post.call_args_list]
        self.assertEqual("".join(chunks), original)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 4000 for chunk in chunks))
        self.assertTrue(chunks[0].endswith("\n\n"))
        self.assertTrue(all(call.kwargs["json"]["receive_id"] == "ou_receiver"
                            and "receive_id_type=open_id" in call.args[0] for call in post.call_args_list))
        token.assert_called_once()

    def test_chunk_failure_stops_without_replaying_delivered_parts(self):
        good, failure = Mock(), Mock()
        failure.text = "offline failure"
        good.json.return_value = {"code": 0}
        failure.json.return_value = {"code": 1, "msg": "offline failure"}
        with patch.object(feishu_app, "get_tenant_access_token", return_value="offline-token"), \
                patch.object(feishu_app.requests, "post", side_effect=[good, failure]) as post:
            with self.assertRaisesRegex(ValueError, "offline failure"):
                feishu_app.send_text("chat", "x" * 12000)
        self.assertEqual(post.call_count, 2)


class FeishuConcurrentDeliveryTests(unittest.TestCase):
    def test_slow_first_reply_serializes_followups_without_blocking_other_chats(self):
        app = object()
        started, release, other_finished = threading.Event(), threading.Event(), threading.Event()
        observed, workers = [], []
        real_thread = threading.Thread

        def create_thread(**kwargs):
            worker = real_thread(**kwargs)
            workers.append(worker)
            return worker

        def process(app, chat, text, sender):
            if text == "first":
                started.set()
                if not release.wait(3):
                    raise AssertionError("first reply never released")
            observed.append(text)
            if text == "other":
                other_finished.set()

        def message(text, chat="chat"):
            return {"type": "text", "chat_id": chat, "open_id": "sender",
                    "message_id": f"concurrent-{text}", "text": text}

        inbound._seen.clear()
        with patch.object(inbound.threading, "Thread", side_effect=create_thread), \
                patch.object(inbound, "_process_text", side_effect=process):
            try:
                inbound.dispatch(app, message("first"))
                self.assertTrue(started.wait(2))
                inbound.dispatch(app, message("second"))
                inbound.dispatch(app, message("third"))
                inbound.dispatch(app, message("other", chat="other-chat"))
                self.assertTrue(other_finished.wait(2), "another chat was blocked by the slow reply")
                self.assertEqual(observed, ["other"])
            finally:
                release.set()
                for worker in workers:
                    worker.join(timeout=3)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(observed, ["other", "first", "second", "third"])
        self.assertFalse(inbound._pending)
        self.assertFalse(inbound._pending_ids)
        inbound._seen.clear()


if __name__ == "__main__":
    unittest.main()
