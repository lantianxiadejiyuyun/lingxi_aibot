import json
from pathlib import Path
import tempfile
import unittest

from dotenv import dotenv_values
from scripts.configure_napcat import initialize


class NapCatConfigTests(unittest.TestCase):
    def test_shared_auth_and_no_public_listener_or_self_messages(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            env = base / ".env"
            env.write_text("SECRET_KEY=keep-this-value\n", encoding="utf-8")
            output = initialize(env, base / "napcat", "ws://app:8000/private/api/v1/integrations/onebot/ws")
            values = dotenv_values(env)
            config = json.loads(output.read_text())
            network = config["network"]
            self.assertEqual(values["SECRET_KEY"], "keep-this-value")
            self.assertGreaterEqual(len(values["ONEBOT_ACCESS_TOKEN"]), 32)
            self.assertEqual(network["httpServers"][0]["token"], values["ONEBOT_ACCESS_TOKEN"])
            self.assertEqual(network["websocketClients"][0]["token"], values["ONEBOT_ACCESS_TOKEN"])
            self.assertFalse(network["websocketClients"][0]["reportSelfMessage"])
            self.assertEqual(network["websocketServers"], [])
            self.assertNotEqual(json.loads((output.parent / "webui.json").read_text())["token"], values["ONEBOT_ACCESS_TOKEN"])
            before = output.read_bytes()
            with self.assertRaises(ValueError):
                initialize(env, base / "napcat", "ws://other/ws")
            self.assertEqual(output.read_bytes(), before)

    def test_invalid_url_and_existing_account_config_are_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            for url in ("http://app/ws", "ws://token@host/ws", "ws://host/ws?token=secret", "ws://host/ws#fragment", "ws://host:999999/ws"):
                with self.assertRaises(ValueError):
                    initialize(base / ".env", base / "napcat", url)
            folder = base / "napcat" / "config"
            folder.mkdir(parents=True)
            (folder / "onebot11_12345.json").write_text("{}")
            with self.assertRaises(ValueError):
                initialize(base / ".env", base / "napcat", "ws://app/ws")
            self.assertFalse((base / ".env").exists())

    def test_invalid_existing_token_is_rejected_without_writes(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            env = base / ".env"
            for token in ("x" * 513, "x" * 32 + " space", "中" * 32, "short"):
                env.write_text("ONEBOT_ACCESS_TOKEN=" + token + "\n", encoding="utf-8")
                before = env.read_bytes()
                with self.assertRaises(ValueError):
                    initialize(env, base / "napcat", "ws://app/ws")
                self.assertEqual(env.read_bytes(), before)
                self.assertFalse((base / "napcat").exists())
