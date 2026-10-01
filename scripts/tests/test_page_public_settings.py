"""Public link roots persist globally without changing the internal page listener."""
import importlib.util
import os
from pathlib import Path
import shutil
from unittest.mock import patch

from dotenv import dotenv_values

from app.models.setting import Setting
from app.services.settings_service import get_setting
from scripts.tests.support import IsolatedAppTestCase


class PagePublicSettingsTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.app.config.update(WTF_CSRF_ENABLED=False, MAIN_PORT=8000, PAGE_PORT=8080,
                               PAGE_HOST="192.168.1.100", PAGE_PUBLIC_BASE_URL="")
        self.user = self.make_user()
        self.client = self.client_for(self.user)
        self.env_path = Path(self.temp_directory.name) / "runtime" / ".env"
        self.env_path.parent.mkdir()
        self.env_path.write_text("SECRET_KEY=isolated-public-url-secret\nPAGE_PORT=8080\n", encoding="utf-8")
        self.fields = {"admin_entry": "", "page_port": "8080", "page_host": "192.168.1.100"}
        for target, kwargs, name in (
            ("app.services.install_service.ENV_PATH", {"new": self.env_path}, None),
            ("app.services.page_site_server.restart", {}, "restart"),
            ("app.services.page_site_server.start_if_needed", {}, "start"),
            ("app.services.page_site_server.status", {"return_value": {"running": True, "port": 8080}}, "site_status"),
            ("app.utils.netinfo.clear_page_reachability_cache", {}, "clear_cache"),
            ("app.utils.netinfo.diagnose_page_reachability", {"return_value": {}}, None),
        ):
            item = patch(target, **kwargs)
            value = item.start()
            self.addCleanup(item.stop)
            if name:
                setattr(self, name, value)

    def save(self, **changes):
        return self.client.post("/settings/pages-domain", data={**self.fields, **changes})

    def test_normalized_public_address_is_global_and_internal_port_stays_8080(self):
        response = self.save(page_public_base_url="  HTTP://8.8.8.8:30079/  ")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(get_setting("page_public_base_url", user_id=0), "http://8.8.8.8:30079")
        self.assertEqual(self.app.config["PAGE_PUBLIC_BASE_URL"], "http://8.8.8.8:30079")
        self.assertEqual(self.app.config["PAGE_PORT"], 8080)
        self.assertEqual(get_setting("page_port", user_id=0), 8080)
        self.assertIsNone(Setting.query.filter_by(user_id=self.user.id, key="page_public_base_url").first())
        self.restart.assert_not_called()
        self.start.assert_not_called()
        self.clear_cache.assert_called_once()
        page = self.client.get(response.location)
        self.assertEqual(page.status_code, 200)
        self.assertIn('value="http://8.8.8.8:30079"', page.text)
        self.assertIn("http://8.8.8.8:30079/webs/html/&lt;slug&gt;", page.text)
        self.assertIn("内部端口已监听", page.text)

    def test_persisted_runtime_env_is_used_on_restart(self):
        self.save(page_public_base_url="https://Pages.Example.com:8443/")
        values = dotenv_values(self.env_path)
        self.assertEqual(values["PAGE_PUBLIC_BASE_URL"], "https://pages.example.com:8443")
        self.assertEqual(values["PAGE_PORT"], "8080")
        module_path = Path(self.temp_directory.name) / "config-copy" / "app" / "config.py"
        module_path.parent.mkdir(parents=True)
        shutil.copyfile(Path(__file__).resolve().parents[2] / "app" / "config.py", module_path)
        with patch.dict(os.environ, {"AIBOT_ENV_FILE": str(self.env_path)}, clear=True):
            spec = importlib.util.spec_from_file_location("public_settings_restart_config", module_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        self.assertEqual(module.Config.PAGE_PUBLIC_BASE_URL, "https://pages.example.com:8443")
        self.assertEqual(module.Config.PAGE_PORT, 8080)

    def test_non_admin_and_unauthenticated_cannot_change_global_address(self):
        member = self.make_user("public-url-member", is_admin=False)
        for client in (self.client_for(member), self.app.test_client()):
            response = client.post("/settings/pages-domain", data={**self.fields, "page_public_base_url": "https://denied.example"})
            self.assertEqual(response.status_code, 302)
            self.assertIsNone(get_setting("page_public_base_url", user_id=0))
        self.restart.assert_not_called()
        self.start.assert_not_called()
        self.clear_cache.assert_not_called()
        self.assertNotIn("PAGE_PUBLIC_BASE_URL", dotenv_values(self.env_path))

    def test_invalid_address_cannot_partially_change_other_settings(self):
        self.save(page_public_base_url="https://original.example")
        original_env = self.env_path.read_bytes()
        self.clear_cache.reset_mock()
        for bad in ("example.com", "https://example.com/path", "javascript:alert(1)", "https://user:pass@example.com", "https://example.com:65536"):
            with self.subTest(address=bad):
                response = self.save(admin_entry="changed", page_port="9090", page_host="changed.example", page_public_base_url=bad)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(self.app.config["ADMIN_ENTRY"], "")
                self.assertEqual(self.app.config["PAGE_PORT"], 8080)
                self.assertEqual(self.app.config["PAGE_HOST"], "192.168.1.100")
                self.assertEqual(get_setting("page_public_base_url", user_id=0), "https://original.example")
                self.assertEqual(self.env_path.read_bytes(), original_env)
        self.restart.assert_not_called()
        self.start.assert_not_called()
        self.clear_cache.assert_not_called()

    def test_legacy_form_preserves_address_but_explicit_empty_clears_it(self):
        self.save(page_public_base_url="https://original.example")
        self.save()
        self.assertEqual(get_setting("page_public_base_url", user_id=0), "https://original.example")
        self.assertEqual(self.app.config["PAGE_PUBLIC_BASE_URL"], "https://original.example")
        self.assertEqual(dotenv_values(self.env_path)["PAGE_PUBLIC_BASE_URL"], "https://original.example")
        self.save(page_public_base_url="")
        self.assertEqual(get_setting("page_public_base_url", user_id=0), "")
        self.assertEqual(self.app.config["PAGE_PUBLIC_BASE_URL"], "")
        self.assertEqual(dotenv_values(self.env_path)["PAGE_PUBLIC_BASE_URL"], "")
        self.assertEqual(self.app.config["PAGE_PORT"], 8080)
        self.restart.assert_not_called()

    def test_internal_port_changes_restart_and_stopped_same_port_can_start(self):
        self.site_status.return_value = {"running": False, "port": 0}
        self.save(page_public_base_url="https://public.example")
        self.start.assert_called_once()
        self.restart.assert_not_called()
        self.start.reset_mock()
        self.site_status.return_value = {"running": True, "port": 8080}
        self.save(page_port="9090", page_public_base_url="https://public.example")
        self.restart.assert_called_once()
        self.start.assert_not_called()
        self.assertEqual(self.app.config["PAGE_PORT"], 9090)

    def test_invalid_existing_port_or_entry_does_not_write_valid_public_address(self):
        for invalid in ({"page_port": "8000"}, {"page_port": "0"}, {"admin_entry": "bad/entry"}):
            with self.subTest(invalid=invalid):
                response = self.save(page_public_base_url="https://valid.example", **invalid)
                self.assertEqual(response.status_code, 302)
                self.assertIsNone(get_setting("page_public_base_url", user_id=0))
                self.assertEqual(self.app.config["PAGE_PUBLIC_BASE_URL"], "")
                self.assertNotIn("PAGE_PUBLIC_BASE_URL", dotenv_values(self.env_path))
        self.clear_cache.assert_not_called()
