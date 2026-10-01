"""Public share URLs remain separate from internal listeners and private pages."""
# 8.8.8.8 is a public-IP classification fixture; HTTP is blocked by support.py.
import socket
import unittest
from unittest.mock import patch

from scripts.tests.support import IsolatedAppTestCase
from flask_login import login_user

from app.services import page_service
from app.services.page_site_server import create_page_site_app
from app.services.settings_service import set_setting
from app.utils import netinfo


class PublicBaseNormalizationTests(unittest.TestCase):
    def test_normalizes_complete_root_addresses(self):
        cases = {
            "": "",
            "  ": "",
            " HTTP://Example.COM:30079/ ": "http://example.com:30079",
            "https://example.com/": "https://example.com",
            "http://8.8.8.8:30079": "http://8.8.8.8:30079",
            "http://[2001:4860:4860::8888]:8080/": "http://[2001:4860:4860::8888]:8080",
            "https://例子.中国/": "https://xn--fsqu00a.xn--fiqs8s",
            "http://localhost:8080": "http://localhost:8080",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(page_service.normalize_page_public_base_url(raw), expected)
        self.assertEqual(page_service.normalize_page_public_base_url(None), "")

    def test_rejects_ambiguous_or_non_root_urls(self):
        cases = (
            "example.com:8080", "//example.com", "ftp://example.com", "http://",
            "http:///example.com", "https://user@example.com", "http://user:pw@example.com",
            "http://@example.com", "http://example.com/webs/html/test", "http://example.com//",
            "http://example.com?", "http://example.com?q=x", "http://example.com#",
            "http://example.com/#frag", "http://example.com:", "http://example.com:0",
            "http://example.com:65536", "http://example.com:abc", "http://example.com:-80",
            "http://exa mple.com", "http://example.com\n", "\thttp://example.com",
            "http://exam\x00ple.com", "http://example.com\x7f", "http://example.com\\path",
            "http://[::1]suffix", "http://[::1", "http://[fe80::1%25eth0]",
            "http://999.999.999.999", "http://-example.com", "http://foo_bar.example",
            "http://%65xample.com", "http://example..com", 8080,
        )
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                page_service.normalize_page_public_base_url(raw)


class PublicPageUrlTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.app.config.update(PAGE_PORT=8080, PAGE_HOST="192.168.1.100",
                               PAGE_PUBLIC_BASE_URL="", ADMIN_ENTRY="lingxi")
        self.user = self.make_user()
        self.public = page_service.create_page(self.user.id, "Public", "<h1>public</h1>", slug="public-check")
        self.private = page_service.create_page(self.user.id, "Private", "<h1>private</h1>",
                                                 slug="private-check", is_public=False)
        netinfo.clear_page_reachability_cache()
        self.addCleanup(netinfo.clear_page_reachability_cache)

    def configure_public(self, value="http://8.8.8.8:30079/"):
        set_setting("page_public_base_url", value, user_id=0)

    def test_external_30079_is_used_without_changing_internal_8080(self):
        self.configure_public()
        self.assertEqual(page_service.page_site_base_url(), "http://8.8.8.8:30079")
        self.assertEqual(page_service.page_port_configured(), 8080)
        self.assertEqual(self.app.config["PAGE_PORT"], 8080)
        with self.app.test_request_context("/lingxi/pages/", base_url="http://192.168.1.100:8000"):
            self.assertEqual(page_service.page_public_url(self.public),
                             "http://8.8.8.8:30079/webs/html/public-check")

    def test_background_and_feishu_contexts_get_full_public_url(self):
        self.configure_public()
        self.assertEqual(page_service.page_public_url(self.public),
                         "http://8.8.8.8:30079/webs/html/public-check")

    def test_private_page_keeps_authenticated_admin_address(self):
        self.configure_public()
        with self.app.test_request_context("/lingxi/pages/", base_url="https://admin.example:8443"):
            self.assertEqual(page_service.page_public_url(self.private),
                             "https://admin.example:8443/lingxi/webs/html/private-check")
        self.assertEqual(page_service.page_public_url(self.private), "/lingxi/webs/html/private-check")
        site_client = create_page_site_app(self.app).test_client()
        self.assertEqual(site_client.get("/webs/html/private-check").status_code, 404)

    def test_explicit_public_address_also_works_without_separate_listener(self):
        self.configure_public("https://pages.example/")
        self.app.config["PAGE_PORT"] = 0
        self.assertEqual(page_service.page_public_url(self.public), "https://pages.example/webs/html/public-check")
        self.assertEqual(page_service.page_port_configured(), 0)

    def test_empty_setting_preserves_legacy_host_and_port_and_request_fallback(self):
        self.configure_public("")
        self.assertEqual(page_service.page_public_url(self.public),
                         "http://192.168.1.100:8080/webs/html/public-check")
        self.app.config["PAGE_PORT"] = 0
        with self.app.test_request_context("/lingxi/pages/", base_url="https://admin.example:8443"):
            self.assertEqual(page_service.page_public_url(self.public),
                             "https://admin.example:8443/webs/html/public-check")
        self.assertEqual(page_service.page_public_url(self.public), "/webs/html/public-check")

    def test_global_setting_overrides_env_and_ignores_user_scoped_value(self):
        self.app.config["PAGE_PUBLIC_BASE_URL"] = "https://env.example/"
        set_setting("page_public_base_url", "https://user.example", user_id=self.user.id)
        self.assertEqual(page_service.page_public_base_url_configured(), "https://env.example")
        self.configure_public("https://global.example/")
        with self.app.test_request_context("/"):
            login_user(self.user)
            self.assertEqual(page_service.page_public_base_url_configured(), "https://global.example")
        self.configure_public("")
        self.assertEqual(page_service.page_public_base_url_configured(), "")

    def test_invalid_manually_written_config_falls_back_without_logging_its_contents(self):
        self.app.config["PAGE_PUBLIC_BASE_URL"] = "https://user:secret@example.com"
        with self.assertLogs("app.services.page_service", level="WARNING") as logs:
            self.assertEqual(page_service.page_site_base_url(), "http://192.168.1.100:8080")
        self.assertNotIn("secret", " ".join(logs.output))

    def test_public_route_accepts_forwarded_host_without_redirect(self):
        self.configure_public()
        client = create_page_site_app(self.app).test_client()
        for host in ("192.168.1.100:8080", "8.8.8.8:30079"):
            with self.subTest(host=host):
                response = client.get("/webs/html/public-check", headers={"Host": host})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_data(as_text=True), "<h1>public</h1>")
                self.assertNotIn("Location", response.headers)

    def test_diagnostics_use_external_host_and_port_without_claiming_connectivity(self):
        self.configure_public()
        with patch.object(netinfo, "_probe_local_ipv4", return_value=(True, "192.168.1.100")), \
                patch.object(netinfo.socket, "getaddrinfo") as dns:
            result = netinfo.diagnose_page_reachability()
        dns.assert_not_called()
        self.assertEqual(result["page_host"], "8.8.8.8")
        self.assertEqual(result["page_port"], 8080)
        self.assertEqual(result["page_public_port"], 30079)
        self.assertTrue(result["pages_openable"])
        self.assertFalse(result["reachability_verified"])
        self.assertIn("未实测", result["diagnostic_note"])

    def test_domain_diagnostic_uses_dns_and_cache_can_be_invalidated(self):
        self.configure_public("https://pages.example")
        address = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch.object(netinfo, "_probe_local_ipv4", return_value=(True, "192.168.1.100")), \
                patch.object(netinfo.socket, "getaddrinfo", return_value=address) as dns:
            first = netinfo.diagnose_page_reachability()
            self.assertTrue(first["pages_openable"])
            self.assertEqual(first["page_public_port"], 443)
            self.configure_public("http://192.168.1.100:30079")
            netinfo.clear_page_reachability_cache()
            second = netinfo.diagnose_page_reachability()
            self.assertFalse(second["pages_openable"])
            self.assertEqual(second["page_host"], "192.168.1.100")
        dns.assert_called_once()

    def test_no_configured_host_uses_one_local_probe_without_recursion(self):
        self.app.config["PAGE_HOST"] = ""
        with patch.object(netinfo, "_probe_local_ipv4", return_value=(True, "192.168.1.100")) as probe:
            diagnostic = netinfo.diagnose_page_reachability()
        probe.assert_called_once()
        self.assertEqual(diagnostic["page_host"], "192.168.1.100")
        self.assertFalse(diagnostic["pages_openable"])


if __name__ == "__main__":
    unittest.main()
