"""Validate the resolved optional media stack without starting Docker services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
DOCKER = shutil.which("docker")


@unittest.skipUnless(DOCKER, "Docker Compose CLI not installed")
class MediaComposeTest(unittest.TestCase):
    def render(self, base):
        with tempfile.TemporaryDirectory(prefix="lingxi-compose-test-") as folder:
            env_file = Path(folder) / "isolated.env"
            env_file.write_text("", encoding="utf-8")
            env = dict(os.environ, MEDIA_ROOT=folder, MYSQL_PASSWORD="test-only", MYSQL_ROOT_PASSWORD="test-only",
                       LINGXI_IMAGE="lingxi-aibot:test-media", MEDIA_ARIA2_SECRET_FILE=str(Path(folder) / "secret"),
                       MEDIA_QB_ADMIN_PORT="18080")
            result = subprocess.run([DOCKER, "compose", "--env-file", str(env_file), "-f", str(ROOT / base),
                                     "-f", str(ROOT / "docker-compose.media.yml"), "--profile", "media-aria2",
                                     "--profile", "media-qb", "--profile", "mysql", "config", "--format", "json"],
                                    env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_quickstart_and_existing_stack_keep_db_network_and_same_image(self):
        for base, db_network in (("docker-compose.quickstart.yml", "default"), ("docker-compose.yml", "aibot")):
            with self.subTest(base=base):
                spec = self.render(base)
                services = spec["services"]
                self.assertEqual(services["app"]["image"], services["media-worker"]["image"])
                self.assertIn(db_network, services["app"]["networks"])
                self.assertIn(db_network, services["media-worker"]["networks"])
                self.assertIn(db_network, services["db"]["networks"])
                self.assertEqual(services["media-worker"]["command"], ["python", "-m", "flask", "media-worker", "--concurrency", "3"])
                self.assertEqual(services["media-worker"]["environment"]["SCHEDULER_ENABLED"], "0")
                self.assertNotIn("ports", services["media-worker"])

    def test_optional_downloaders_preserve_mount_and_network_boundaries(self):
        services = self.render("docker-compose.quickstart.yml")["services"]
        sources = []
        for name in ("app", "media-worker", "aria2", "qbittorrent"):
            mount = next(v for v in services[name]["volumes"] if v["target"] == "/media")
            self.assertFalse(mount["bind"]["create_host_path"])
            sources.append(mount["source"])
        self.assertEqual(len(set(sources)), 1)
        for name in ("aria2", "qbittorrent"):
            service = services[name]
            self.assertEqual(set(service["networks"]), {"media"})
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertIn("NET_ADMIN", service["cap_add"])
            self.assertTrue(service["read_only"])
            self.assertEqual(service["sysctls"]["net.ipv6.conf.all.disable_ipv6"], "1")
            self.assertTrue(service["profiles"])
        self.assertNotIn("ports", services["aria2"])
        self.assertTrue(all(port["host_ip"] == "127.0.0.1" for port in services["qbittorrent"]["ports"]))


if __name__ == "__main__":
    unittest.main()
