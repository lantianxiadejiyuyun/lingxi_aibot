"""Run the existing local tests against an isolated application instead of run.py."""
import contextlib
import io
from pathlib import Path
import runpy
import sys
import types
from unittest.mock import patch

from scripts.tests.support import IsolatedAppTestCase


class ExistingRegressionTests(IsolatedAppTestCase):
    def run_existing(self, filename):
        self.make_user()
        output = io.StringIO()
        exit_code = 0
        module = types.ModuleType("run")
        module.app = self.app
        script = Path(__file__).resolve().parents[1] / filename
        with patch.dict(sys.modules, {"run": module}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                namespace = runpy.run_path(str(script), run_name="__main__")
                self.assertFalse(namespace.get("FAILED"), output.getvalue())
            except SystemExit as exc:
                exit_code = exc.code or 0
        self.assertEqual(exit_code, 0, output.getvalue())

    def test_cli(self):
        self.run_existing("test_cli.py")

    def test_failed_session_recovery(self):
        self.run_existing("test_db_session.py")

    def test_llm_protocol_and_account_settings(self):
        self.run_existing("test_llm_protocol.py")

    def test_persona_and_streaming_ack(self):
        self.run_existing("test_persona.py")
