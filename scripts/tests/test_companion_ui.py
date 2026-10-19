"""Companion page: isolated account settings, no sends, and safe activity rendering."""
import re
from datetime import timedelta
from unittest.mock import patch

from scripts.tests.support import IsolatedAppTestCase

from app.extensions import db
from app.models.conversation import Conversation
from app.models.companion import CompanionActivity
from app.services import companion_service
from app.utils.timeutil import utcnow


class CompanionPageTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        if "companion" not in self.app.blueprints:
            from app.blueprints.companion import bp
            self.app.register_blueprint(bp)
        self.user = self.make_user()
        self.other = self.make_user("companion_other")
        self.client = self.client_for(self.user)

    def token(self, client=None):
        response = (client or self.client).get("/companion/")
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)

    def form(self, **changes):
        values = {
            "csrf_token": self.token(), "enabled": "1",
            "active_start": "09:00", "active_end": "22:00",
            "quiet_start": "23:00", "quiet_end": "08:00",
            "min_interval_minutes": "180", "daily_limit": "3",
            "chat_cooldown_minutes": "45", "channels": ["inapp"],
            "interests": "动画\n音乐", "share_bilibili": "1",
        }
        values.update(changes)
        return values

    def test_page_and_mutations_require_login(self):
        anonymous = self.app.test_client()
        with patch("app.blueprints.companion.companion_service.get_view") as view, \
                patch("app.blueprints.companion.companion_service.save_config") as save, \
                patch("app.blueprints.companion.astrbot_service.unbind") as unbind:
            self.assertEqual(anonymous.get("/companion/").status_code, 302)
            # Missing CSRF may be rejected before authentication, but never reaches services.
            self.assertIn(anonymous.post("/companion/save").status_code, (302, 400))
            self.assertIn(anonymous.post("/companion/astrbot/unbind").status_code, (302, 400))
            view.assert_not_called()
            save.assert_not_called()
            unbind.assert_not_called()

    def test_csrf_required_for_save_and_unbind(self):
        with patch("app.blueprints.companion.companion_service.save_config") as save, \
                patch("app.blueprints.companion.astrbot_service.unbind") as unbind:
            for endpoint in ("/companion/save", "/companion/astrbot/unbind"):
                self.assertEqual(self.client.post(endpoint, data={"csrf_token": "forged"}).status_code, 400)
                self.assertEqual(self.client.post(endpoint).status_code, 400)
            save.assert_not_called()
            unbind.assert_not_called()

    def test_save_updates_only_logged_in_account_and_does_not_send(self):
        before_other = companion_service.load_config(self.other)
        with patch("app.services.notify_service.notify") as send:
            response = self.client.post("/companion/save", data=self.form(user_id=str(self.other.id)))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/companion/")
        actual = companion_service.load_config(self.user)
        self.assertTrue(actual["enabled"])
        self.assertTrue(actual["share_bilibili"])
        self.assertEqual(actual["interests"], ["动画", "音乐"])
        self.assertEqual(actual["channels"], ["inapp"])
        self.assertEqual(companion_service.load_config(self.other), before_other)
        send.assert_not_called()
        self.assertEqual(self.client_for(self.other).get("/companion/").status_code, 200)

    def test_invalid_settings_preserve_previous_values(self):
        self.client.post("/companion/save", data=self.form())
        before = companion_service.load_config(self.user)
        for changes in ({"daily_limit": "0"}, {"active_start": "25:99"},
                        {"channels": ["not-a-channel"]}, {"interests": "x" * 41},
                        {"min_interval_minutes": "NaN"}):
            with self.subTest(changes=changes):
                response = self.client.post("/companion/save", data=self.form(**changes), follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(companion_service.load_config(self.user), before)

    def test_unchecked_switches_are_saved_as_false(self):
        values = self.form()
        self.client.post("/companion/save", data=values)
        values = self.form()
        values.pop("enabled")
        values.pop("share_bilibili")
        self.client.post("/companion/save", data=values)
        actual = companion_service.load_config(self.user)
        self.assertFalse(actual["enabled"])
        self.assertFalse(actual["share_bilibili"])

    def test_real_settings_and_activity_roundtrip_reloads_the_saved_account(self):
        # Use the real service and ORM on both requests; no generated view fixture.
        initial = self.client.get("/companion/").get_data(as_text=True)
        self.assertIn("陪伴未开启", initial)
        response = self.client.post("/companion/save", data=self.form(
            active_start="10:15", active_end="20:45", daily_limit="2",
            min_interval_minutes="240", interests="天文\n摄影",
        ))
        self.assertEqual(response.status_code, 302)
        mine = Conversation(user_id=self.user.id, title="灵犀日常")
        db.session.add(mine)
        db.session.flush()
        db.session.add(CompanionActivity(
            user_id=self.user.id, dedupe_key="ui-roundtrip-mine", status="sent",
            reason="sharing", body="今天发现一段星空记录。", conversation_id=mine.id,
            sources=[{"id": "bili:av123", "kind": "bilibili_search", "title": "星空资料",
                      "url": "https://www.bilibili.com/video/av123"}],
        ))
        db.session.add(CompanionActivity(
            user_id=self.other.id, dedupe_key="ui-roundtrip-other", status="sent",
            reason="sharing", body="另一账号的私有内容", sources=[],
        ))
        db.session.commit()
        response = self.client.get("/companion/")
        text = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("陪伴已开启", text)
        self.assertRegex(text, r'name="active_start" value="10:15"')
        self.assertRegex(text, r'name="active_end" value="20:45"')
        self.assertRegex(text, r'name="daily_limit"[^>]*value="2"')
        self.assertRegex(text, r'name="min_interval_minutes"[^>]*value="240"')
        self.assertIn("天文\n摄影</textarea>", text)
        self.assertIn("今天发现一段星空记录。", text)
        self.assertIn('href="https://www.bilibili.com/video/av123/"', text)
        self.assertIn(f'/chat/?conversation={mine.id}', text)
        self.assertNotIn("另一账号的私有内容", text)
        other_text = self.client_for(self.other).get("/companion/").get_data(as_text=True)
        self.assertIn("陪伴未开启", other_text)
        self.assertIn("另一账号的私有内容", other_text)
        self.assertNotIn("今天发现一段星空记录。", other_text)

    def test_unbind_uses_current_account_and_never_get(self):
        csrf = self.token()
        with patch("app.blueprints.companion.astrbot_service.unbind") as unbind:
            self.assertEqual(self.client.get("/companion/astrbot/unbind").status_code, 405)
            response = self.client.post("/companion/astrbot/unbind", data={
                "csrf_token": csrf, "user_id": self.other.id,
            })
            self.assertEqual(response.status_code, 302)
            unbind.assert_called_once_with(self.user.id)

    def test_live_and_interrupted_activities_have_clear_status_and_single_active_nav(self):
        now = utcnow()
        for key, status, reason, body, created in (
            ("working", "evaluating", "preparing", "", now),
            ("interrupted", "evaluating", "preparing", "", now - timedelta(hours=1)),
            ("unknown", "dispatching", "sharing", "待确认送达的正文", now - timedelta(hours=1)),
        ):
            db.session.add(CompanionActivity(user_id=self.user.id, dedupe_key=key,
                status=status, reason=reason, body=body, sources=[], created_at=created))
        db.session.commit()
        text = self.client.get("/companion/").get_data(as_text=True)
        self.assertIn("正在判断", text)
        self.assertIn("这次判断中断了", text)
        self.assertIn("送达状态未知", text)
        self.assertIn("待确认送达的正文", text)
        self.assertIn("为避免重复，没有自动补发", text)
        links = re.findall(r'<a href="([^"]+)" class="([^"]*)"', text)
        active = [href for href, classes in links if "nav-item" in classes.split() and "active" in classes.split()]
        self.assertEqual(active, ["/companion/"])

    def test_activity_text_is_escaped_and_only_owned_chat_links_are_rendered(self):
        mine = Conversation(user_id=self.user.id, title="mine")
        foreign = Conversation(user_id=self.other.id, title="foreign")
        db.session.add_all([mine, foreign])
        db.session.commit()
        values = {
            "config": companion_service.load_config(self.user), "state": {},
            "activities": [{"status": "sent", "reason": "",
                            "body": '<script>alert("body")</script>',
                            "sources": [{"title": '<img src=x onerror="bad()">',
                                         "url": "https://www.bilibili.com/video/BV1234567890/?foo=secret"},
                                        {"title": "bad script link", "url": "javascript:alert(1)"},
                                        {"title": "fake bilibili", "url": "https://www.bilibili.com.evil.test/video/BV1234567890/"},
                                        {"title": "credentials", "url": "https://password@www.bilibili.com/video/BV1234567890/"}],
                            "created_at": "2026-10-19T12:30:00+08:00", "conversation_id": mine.id},
                           {"status": "skip", "body": "other ref", "sources": [],
                            "conversation_id": foreign.id}],
        }
        binding = {"bound": True, "enabled": True, "session": "<svg onload=alert(1)>",
                   "api_token": "PRIVATE-TOKEN-MUST-NOT-RENDER", "conversation_id": foreign.id}
        captured_users = []
        def own_view(user, **kwargs):
            captured_users.append(user.id)
            return values
        with patch("app.blueprints.companion.companion_service.get_view", side_effect=own_view), \
                patch("app.blueprints.companion.astrbot_service.binding_view", return_value=binding) as bind:
            response = self.client.get("/companion/")
        text = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured_users, [self.user.id])
        self.assertGreaterEqual(bind.call_count, 1)
        self.assertTrue(all(call.args == (self.user.id,) for call in bind.call_args_list))
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&lt;img", text)
        self.assertNotIn('<script>alert("body")', text)
        self.assertNotIn('<svg onload=', text)
        self.assertNotIn("PRIVATE-TOKEN-MUST-NOT-RENDER", text)
        self.assertNotIn('href="javascript:', text)
        self.assertNotIn('href="https://www.bilibili.com.evil.test', text)
        self.assertNotIn('href="https://password@', text)
        self.assertIn('href="https://www.bilibili.com/video/BV1234567890/"', text)
        self.assertNotIn("foo=secret", text)
        self.assertIn(f'/chat/?conversation={mine.id}', text)
        self.assertNotIn(f'/chat/?conversation={foreign.id}', text)
        self.assertNotIn("立即发送", text)
        self.assertNotIn("测试发送", text)


if __name__ == "__main__":
    import unittest
    unittest.main()
