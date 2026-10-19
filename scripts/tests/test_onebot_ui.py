import re
from unittest.mock import patch

from scripts.tests.support import IsolatedAppTestCase
from app.models.onebot import OneBotBindingCode
from app.utils.integration_api import ApiError


class OneBotPageTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.other = self.make_user("qq_other", is_admin=False)
        self.client = self.client_for(self.user)
        self.app.config.update(ONEBOT_ENABLED=True, ONEBOT_ACCESS_TOKEN="private-test-token-" + "x" * 32,
                               ONEBOT_API_URL="http://napcat:3000", ONEBOT_WEBUI_URL="http://nas:6099/webui")

    def csrf(self, client=None):
        response = (client or self.client).get("/settings/qq/")
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)

    def test_login_and_csrf_required(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/settings/qq/").status_code, 302)
        for route in ("/settings/qq/code", "/settings/qq/unbind"):
            self.assertEqual(self.client.post(route).status_code, 400)
            self.assertEqual(self.client.post(route, data={"csrf_token": "invalid"}).status_code, 400)

    def test_read_never_creates_code_or_sends_and_hides_token(self):
        with patch("app.services.onebot_transport.send_private") as send:
            response = self.client.get("/settings/qq/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertNotIn(self.app.config["ONEBOT_ACCESS_TOKEN"], response.get_data(as_text=True))
        self.assertEqual(OneBotBindingCode.query.count(), 0)
        send.assert_not_called()

    def test_generation_is_account_scoped_and_no_token_or_api_url(self):
        with patch("app.services.onebot_transport.send_private") as send:
            response = self.client.post("/settings/qq/code", data={"csrf_token": self.csrf(), "user_id": self.other.id})
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("/lingxi 绑定", html)
        self.assertNotIn(self.app.config["ONEBOT_ACCESS_TOKEN"], html)
        self.assertNotIn("http://napcat:3000", html)
        self.assertEqual(OneBotBindingCode.query.filter_by(user_id=self.user.id).count(), 1)
        self.assertEqual(OneBotBindingCode.query.filter_by(user_id=self.other.id).count(), 0)
        self.assertNotIn("/lingxi 绑定 ", self.client.get("/settings/qq/").get_data(as_text=True))
        send.assert_not_called()

    def test_nonadmin_does_not_get_napcat_admin_link(self):
        response = self.client_for(self.other).get("/settings/qq/")
        self.assertNotIn("http://nas:6099", response.get_data(as_text=True))

    def test_unsafe_webui_url_is_not_rendered(self):
        for value in ("javascript:alert(1)", "http://admin:secret@nas:6099"):
            self.app.config["ONEBOT_WEBUI_URL"] = value
            self.assertNotIn(value, self.client.get("/settings/qq/").get_data(as_text=True))

    def test_disabled_connector_does_not_issue_code(self):
        token = self.csrf()
        self.app.config["ONEBOT_ENABLED"] = False
        response = self.client.post("/settings/qq/code", data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(OneBotBindingCode.query.count(), 0)

    def test_busy_unbind_is_visible_and_does_not_become_server_error(self):
        token = self.csrf()
        with patch("app.services.onebot_service.unbind", side_effect=ApiError("QQ 绑定或投递繁忙", 503, "onebot_busy")):
            response = self.client.post("/settings/qq/unbind", data={"csrf_token": token}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("QQ 绑定或投递繁忙", response.get_data(as_text=True))
