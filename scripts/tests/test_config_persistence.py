"""隔离配置回归测试：不连接数据库，不读取或写入实际运行配置。

运行：python -m unittest discover -s scripts/tests -p test_config_persistence.py -v
"""
import importlib.util
import os
from pathlib import Path
import shutil
import shlex
import tempfile
import unittest
from unittest.mock import patch

from dotenv import set_key
import click
from click.testing import CliRunner
from flask import Flask, current_app
from flask.cli import FlaskGroup, with_appcontext


ROOT = Path(__file__).resolve().parents[2]


class ConfigPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.module_path = self.root / "app" / "config.py"
        self.module_path.parent.mkdir()
        shutil.copyfile(ROOT / "app" / "config.py", self.module_path)

    def tearDown(self):
        self.temp.cleanup()

    def load_config(self, env_path=None):
        variables = {"AIBOT_ENV_FILE": str(env_path)} if env_path else {}
        with patch.dict(os.environ, variables, clear=True):
            return self.import_config()

    def import_config(self):
        spec = importlib.util.spec_from_file_location("lingxi_config_test", self.module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_first_import_then_atomic_save_survives_restart(self):
        legacy = self.root / ".env"
        original = (
            "SECRET_KEY=please-generate-a-random-50-char-string\n"
            "SESSION_COOKIE_SECURE=1\nMYSQL_HOST=legacy-db\n"
        )
        legacy.write_text(original, encoding="utf-8")
        runtime = self.root / "data" / ".env"

        first = self.load_config(runtime)
        self.assertEqual(first.ENV_PATH, runtime)
        self.assertEqual(first.Config.MYSQL_HOST, "legacy-db")
        self.assertTrue(first.Config.SESSION_COOKIE_SECURE)
        self.assertNotEqual(first.Config.SECRET_KEY, "please-generate-a-random-50-char-string")
        if os.name != "nt":
            self.assertEqual(runtime.stat().st_mode & 0o777, 0o600)

        # 与安装向导相同的原子写入；目标处于目录挂载内而非单文件挂载点。
        set_key(runtime, "MYSQL_HOST", "updated-db")
        restarted = self.load_config(runtime)
        self.assertEqual(restarted.Config.MYSQL_HOST, "updated-db")
        self.assertEqual(restarted.Config.SECRET_KEY, first.Config.SECRET_KEY)
        self.assertEqual(legacy.read_text(encoding="utf-8"), original)

    def test_existing_runtime_config_is_never_overwritten_by_seed(self):
        (self.root / ".env").write_text("SECRET_KEY=legacy-secret\nMYSQL_HOST=legacy-db\n", encoding="utf-8")
        runtime = self.root / "data" / ".env"
        runtime.parent.mkdir()
        runtime.write_text("SECRET_KEY=runtime-secret\nMYSQL_HOST=runtime-db\n", encoding="utf-8")

        config = self.load_config(runtime)
        self.assertEqual(config.Config.SECRET_KEY, "runtime-secret")
        self.assertEqual(config.Config.MYSQL_HOST, "runtime-db")

    def test_local_deployment_keeps_project_root_env(self):
        legacy = self.root / ".env"
        legacy.write_text("SECRET_KEY=local-secret\nMYSQL_HOST=local-db\n", encoding="utf-8")
        config = self.load_config()

        self.assertEqual(config.ENV_PATH, legacy)
        self.assertEqual(config.Config.MYSQL_HOST, "local-db")
        self.assertFalse((self.root / "data" / ".env").exists())

    def test_docker_flask_cli_reads_runtime_config(self):
        (self.root / ".env").write_text("SECRET_KEY=seed-secret\nMYSQL_HOST=seed-db\n", encoding="utf-8")
        runtime = self.root / "data" / ".env"
        runtime.parent.mkdir()
        runtime.write_text("SECRET_KEY=runtime-secret\nMYSQL_HOST=runtime-db\nPAGE_BIND=127.0.0.1\n", encoding="utf-8")

        # 使用镜像声明的真实环境变量，验证 Flask CLI 不会预加载旧种子配置。
        docker_env = {}
        for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", "").splitlines():
            if line.startswith("ENV "):
                docker_env.update(item.split("=", 1) for item in shlex.split(line[4:]))
        docker_env["AIBOT_ENV_FILE"] = str(runtime)
        docker_env.pop("FLASK_APP", None)

        def create_app():
            app = Flask("config-test")
            app.config.from_object(self.import_config().Config)
            return app

        @click.command("configured-host")
        @with_appcontext
        def configured_host():
            click.echo(f"{current_app.config['MYSQL_HOST']} {current_app.config['PAGE_BIND']} {current_app.config['MAIN_PORT']}")

        previous_directory = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.dict(os.environ, docker_env, clear=True):
                group = FlaskGroup(create_app=create_app)
                group.add_command(configured_host)
                result = CliRunner().invoke(group, ["configured-host"])
            self.assertEqual(result.exit_code, 0, str(result.exception))
            self.assertIn("runtime-db 0.0.0.0 8000", result.output)
        finally:
            os.chdir(previous_directory)


if __name__ == "__main__":
    unittest.main()
