"""Offline integration checks for scoped pages, entry changes and private images."""
import re
from unittest.mock import patch

from scripts.tests.support import IsolatedAppTestCase

from app import apply_admin_entry_config
from app.extensions import db
from app.models.image import ImageAsset


class AdminEntryTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        network = patch("app.utils.netinfo.diagnose_page_reachability", return_value={})
        network.start()
        self.addCleanup(network.stop)

    def configure_entry(self, entry):
        apply_admin_entry_config(self.app, entry)
        return self.client_for(self.user)

    def csrf_token(self, response):
        self.assertEqual(response.status_code, 200)
        return re.search(r'<meta name="csrf-token" content="([^"]+)"',
                         response.get_data(as_text=True)).group(1)

    def save_entry(self, client, old_entry, new_entry, restart_error=None):
        prefix = f"/{old_entry}" if old_entry else ""
        page = client.get(f"{prefix}/settings/")
        token = self.csrf_token(page)
        with patch("app.services.install_service._ensure_env_file"), \
                patch("dotenv.set_key"), \
                patch("app.services.page_site_server.restart", side_effect=restart_error):
            return client.post(f"{prefix}/settings/pages-domain", data={
                "csrf_token": token, "admin_entry": new_entry,
                "page_port": "8080" if restart_error else "", "page_host": "",
            })

    def test_scoped_page_navigation_and_authenticated_action(self):
        client = self.configure_entry("secret")
        response = client.get("/secret/tasks/")
        token = self.csrf_token(response)
        html = response.get_data(as_text=True)
        self.assertIn('data-script-root="/secret"', html)
        self.assertIn('href="/secret/chat/"', html)
        self.assertIn('action="/secret/tasks/"', html)
        shared = html.index("js/app.js")
        self.assertLess(shared, html.index("window.api.post("))
        response = client.post("/secret/tasks/api/create", json={"title": "Scoped action"},
                               headers={"X-CSRFToken": token})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(client.post("/tasks/api/create", json={"title": "hidden"}).status_code, 404)

    def test_entry_matching_a_blueprint_keeps_its_full_route(self):
        for entry in ("chat", "tasks", "settings"):
            with self.subTest(entry=entry):
                client = self.configure_entry(entry)
                page = client.get(f"/{entry}/tasks/")
                token = self.csrf_token(page)
                self.assertIn(f'data-script-root="/{entry}"', page.get_data(as_text=True))
                response = client.post(f"/{entry}/tasks/api/create",
                                       json={"title": f"Action under {entry}"},
                                       headers={"X-CSRFToken": token})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["ok"])
                response = client.get(f"/{entry}/chat/api/conversations")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["ok"])

    def test_changing_entry_redirects_and_preserves_session_at_new_cookie_path(self):
        for old_entry, new_entry in (("", "secret"), ("old", "new"), ("old", "")):
            with self.subTest(old_entry=old_entry, new_entry=new_entry):
                client = self.configure_entry(old_entry)
                response = self.save_entry(client, old_entry, new_entry)
                prefix = f"/{new_entry}" if new_entry else ""
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], f"{prefix}/settings/?tab=pages-domain")
                self.assertEqual(self.app.config["SESSION_COOKIE_PATH"], prefix or "/")
                cookie = client.get_cookie("session", path=prefix or "/")
                self.assertIsNotNone(cookie)
                self.assertEqual(cookie.path, prefix or "/")
                page = client.get(response.headers["Location"])
                self.assertEqual(page.status_code, 200)
                self.assertIn(self.user.username, page.get_data(as_text=True))
                if old_entry and old_entry != new_entry and new_entry:
                    self.assertEqual(client.get(f"/{old_entry}/settings/").status_code, 404)

    def test_saved_entry_redirects_to_new_path_even_if_page_server_restart_fails(self):
        client = self.configure_entry("old")
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.save_entry(client, "old", "new", RuntimeError("isolated restart failure"))
        self.assertEqual(response.headers["Location"], "/new/settings/?tab=pages-domain")
        page = client.get(response.headers["Location"])
        self.assertEqual(page.status_code, 200)
        self.assertIn("网页端口启动失败", page.get_data(as_text=True))

    def test_private_image_uses_scoped_session_while_public_image_stays_unscoped(self):
        client = self.configure_entry("secret")
        directory = self.app.config["IMAGE_DIR"]
        directory.mkdir(parents=True)
        assets = []
        for i, public in enumerate((False, True), start=1):
            asset = ImageAsset(user_id=self.user.id, file_path=f"img-12345678-{i}.png",
                               prompt="Offline fixture", is_public=public)
            db.session.add(asset)
            (directory / asset.file_path).write_bytes(b"isolated-image-fixture")
            assets.append(asset)
        db.session.commit()
        page = client.get("/secret/images/")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        private_url = f"/secret/img/{assets[0].file_path}"
        public_url = f"/img/{assets[1].file_path}"
        self.assertIn(f"http://localhost{private_url}", html)
        self.assertIn(f"http://localhost{public_url}", html)
        response = client.get(private_url)
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_data(), b"isolated-image-fixture")
        finally:
            response.close()
        self.assertEqual(client.get(f"/img/{assets[0].file_path}").status_code, 404)
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get(private_url).status_code, 404)
        response = anonymous.get(public_url)
        try:
            self.assertEqual(response.status_code, 200)
        finally:
            response.close()
