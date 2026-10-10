import os
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from typer.testing import CliRunner

from tests.connect_support import KEY, clean_environ, closed_port, fake_daemon, make_home
from zhunt.cli import app
from zhunt.doctor import Status, run_checks
from zhunt.installer import Installer

SECRET_VALUE = "sk-groq-super-secret-value"

CATALOG = """
providers:
  groq:
    name: Groq
    base_url: https://api.groq.com/openai/v1
    key_env: GROQ_API_KEY
    verified_on: {groq}
    models:
      - id: m1
        tiers: [chat]
        intelligence: 50
        speed: 50
        context_window: 1000
        supports_tools: true
        may_train_on_prompts: false
  other:
    name: Other
    base_url: https://example.test/v1
    key_env: OTHER_API_KEY
    verified_on: {other}
    models:
      - id: m2
        tiers: [chat]
        intelligence: 50
        speed: 50
        context_window: 1000
        supports_tools: true
        may_train_on_prompts: false
"""
TODAY = date(2026, 10, 10)


class DoctorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.catalog = self.root / "catalog.yaml"
        self.write_catalog(groq=TODAY, other=TODAY)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def write_catalog(self, *, groq: date, other: date) -> None:
        self.catalog.write_text(CATALOG.format(groq=groq.isoformat(), other=other.isoformat()))

    def checks(self, home: Path, port: int, environ=None, **kwargs):
        return {
            check.name: check
            for check in run_checks(
                home=home,
                port=port,
                catalog_path=self.catalog,
                environ={"GROQ_API_KEY": SECRET_VALUE} if environ is None else environ,
                today=TODAY,
                probe=lambda _port: True,
                cwd=self.root,
                **kwargs,
            )
        }


class HealthCheckTests(DoctorTestCase):
    def test_healthy_install_is_all_ok(self) -> None:
        home = make_home(self.root, port=4000)

        checks = self.checks(home, 4000)

        self.assertEqual({c.status for c in checks.values()}, {Status.OK})
        self.assertIn("GROQ_API_KEY", checks["provider keys"].message)

    def test_unreachable_daemon_fails_with_start_command(self) -> None:
        home = make_home(self.root, port=4000)

        checks = {
            c.name: c
            for c in run_checks(
                home=home, port=4000, catalog_path=self.catalog,
                environ={"GROQ_API_KEY": "x"}, today=TODAY, probe=lambda _p: False, cwd=self.root,
            )
        }

        self.assertEqual(checks["daemon"].status, Status.FAIL)
        self.assertIn("zhunt serve", checks["daemon"].fix)

    def test_missing_master_key_fails(self) -> None:
        home = make_home(self.root, port=4000, key=None)

        checks = self.checks(home, 4000)

        self.assertEqual(checks["master key"].status, Status.FAIL)
        self.assertIn("zhunt serve", checks["master key"].fix)

    def test_provider_keys_report_names_only_and_fail_when_none_set(self) -> None:
        home = make_home(self.root, port=4000)

        some = self.checks(home, 4000)["provider keys"]
        none = self.checks(home, 4000, environ={})["provider keys"]

        self.assertEqual(some.status, Status.OK)
        self.assertIn("set: GROQ_API_KEY", some.message)
        self.assertIn("missing: OTHER_API_KEY", some.message)
        self.assertNotIn(SECRET_VALUE, some.message + some.fix)
        self.assertEqual(none.status, Status.FAIL)
        self.assertIn("set: none", none.message)

    def test_provider_keys_are_read_from_the_env_file_too(self) -> None:
        home = make_home(self.root, port=4000, extra=f'OTHER_API_KEY="{SECRET_VALUE}"\n')

        check = self.checks(home, 4000, environ={})["provider keys"]

        self.assertEqual(check.status, Status.OK)
        self.assertIn("set: OTHER_API_KEY", check.message)

    def test_policy_flags_are_reported_as_set(self) -> None:
        home = make_home(self.root, port=4000)

        off = self.checks(home, 4000, environ={"GROQ_API_KEY": "x"})
        on = self.checks(
            home, 4000,
            environ={"GROQ_API_KEY": "x", "ZHUNT_ALLOW_PAID": "1", "ZHUNT_ALLOW_TRAINING": "maybe"},
        )

        self.assertEqual(off["ZHUNT_ALLOW_PAID"].status, Status.OK)
        self.assertIn("unset", off["ZHUNT_ALLOW_PAID"].message)
        self.assertEqual(on["ZHUNT_ALLOW_PAID"].status, Status.WARN)
        self.assertIn("paid", on["ZHUNT_ALLOW_PAID"].message)
        self.assertEqual(on["ZHUNT_ALLOW_TRAINING"].status, Status.WARN)
        self.assertIn("unrecognised", on["ZHUNT_ALLOW_TRAINING"].message)

    def test_catalog_older_than_sixty_days_warns(self) -> None:
        home = make_home(self.root, port=4000)

        self.write_catalog(groq=TODAY - timedelta(days=60), other=TODAY)
        at_limit = self.checks(home, 4000)["catalog"]
        self.write_catalog(groq=TODAY - timedelta(days=61), other=TODAY)
        past_limit = self.checks(home, 4000)["catalog"]

        self.assertEqual(at_limit.status, Status.OK)
        self.assertEqual(past_limit.status, Status.WARN)
        self.assertIn("groq (61d)", past_limit.message)
        self.assertNotIn("other", past_limit.message)

    def test_broken_catalog_fails(self) -> None:
        home = make_home(self.root, port=4000)
        self.catalog.write_text("providers: nope\n")

        checks = self.checks(home, 4000)

        self.assertEqual(checks["catalog"].status, Status.FAIL)


class DriftTests(DoctorTestCase):
    def install(self, home: Path, app_name: str, port: int, mode: str = "api") -> Installer:
        installer = Installer(home=home, platform_name="darwin")
        installer.install(app_name, mode=mode, base_url=f"http://127.0.0.1:{port}")
        return installer

    def test_matching_v1_configs_are_ok(self) -> None:
        home = make_home(self.root, port=4000)
        for name in ("claude", "codex", "hermes", "vscode"):
            self.install(home, name, 4000)

        checks = self.checks(home, 4000, environ={"GROQ_API_KEY": "x"})

        for name in ("claude", "codex", "hermes", "vscode"):
            self.assertEqual(checks[f"app {name}"].status, Status.OK, name)

    def test_port_change_is_reported_with_exact_values_for_every_app(self) -> None:
        home = make_home(self.root, port=4000)
        for name in ("claude", "codex", "hermes", "vscode"):
            self.install(home, name, 4000)

        checks = self.checks(home, 4555, environ={"GROQ_API_KEY": "x"})

        claude = checks["app claude"]
        self.assertEqual(claude.status, Status.WARN)
        self.assertIn(
            "env.ANTHROPIC_BASE_URL: expected http://127.0.0.1:4555, found http://127.0.0.1:4000",
            claude.message,
        )
        self.assertIn("zhunt launch claude", claude.fix)
        self.assertIn(
            "model_providers.zhunt.base_url: expected http://127.0.0.1:4555/v1, "
            "found http://127.0.0.1:4000/v1",
            checks["app codex"].message,
        )
        self.assertIn("base_url: expected http://127.0.0.1:4555/v1", checks["app hermes"].message)
        self.assertIn("url: expected http://127.0.0.1:4555/v1/chat/completions", checks["app vscode"].message)

    def test_rotated_master_key_is_drift_but_no_key_value_is_printed(self) -> None:
        home = make_home(self.root, port=4000, key=None)
        self.install(home, "claude", 4000)  # creates a key in the temp home
        env_file = home / ".zhunt" / "env"
        old_key = [
            line.split("=", 1)[1].strip('"')
            for line in env_file.read_text().splitlines()
            if line.startswith("ZHUNT_MASTER_KEY")
        ][0]
        env_file.write_text(f'ZHUNT_MASTER_KEY="{KEY}"\n')

        check = self.checks(home, 4000, environ={"GROQ_API_KEY": "x"})["app claude"]

        self.assertEqual(check.status, Status.WARN)
        self.assertIn("ANTHROPIC_AUTH_TOKEN", check.message)
        self.assertIn("a different value", check.message)
        self.assertNotIn("ANTHROPIC_BASE_URL", check.message)
        for secret in (old_key, KEY):
            self.assertNotIn(secret, check.render())

    def test_missing_file_and_removed_entry_are_reported(self) -> None:
        home = make_home(self.root, port=4000)
        installer = self.install(home, "claude", 4000)
        self.install(home, "hermes", 4000)
        (home / ".claude" / "settings.json").unlink()
        (home / ".hermes" / "config.yaml").write_text("custom_providers: []\n")

        checks = self.checks(home, 4000, environ={"GROQ_API_KEY": "x"})

        self.assertIn("missing", checks["app claude"].message)
        self.assertIn("settings.json", checks["app claude"].message)
        self.assertIn("name=zhunt", checks["app hermes"].message)
        self.assertEqual(checks["app hermes"].status, Status.WARN)
        self.assertIsNotNone(installer)

    def test_apps_zhunt_never_configured_are_not_checked(self) -> None:
        home = make_home(self.root, port=4000)

        checks = self.checks(home, 4000)

        self.assertFalse([name for name in checks if name.startswith("app ")])

    def test_settings_that_override_launch_are_warned_even_without_a_v1_install(self) -> None:
        home = make_home(self.root, port=4000)
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text(
            '{"env": {"ANTHROPIC_AUTH_TOKEN": "sk-other"}}'
        )

        checks = self.checks(home, 4000)

        self.assertEqual(checks["launch claude"].status, Status.WARN)
        self.assertNotIn("sk-other", checks["launch claude"].render())


class DoctorCommandTests(DoctorTestCase):
    def invoke(self, home: Path, port: int, **extra):
        env = clean_environ(home, GROQ_API_KEY=SECRET_VALUE, **extra)
        with patch.dict(os.environ, env, clear=True):
            return CliRunner().invoke(app, ["doctor", "--port", str(port), "--catalog", str(self.catalog)])

    def test_exit_zero_and_ok_lines_when_healthy(self) -> None:
        with fake_daemon() as port:
            home = make_home(self.root, port=port)
            result = self.invoke(home, port)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("OK    daemon", result.output)
        self.assertIn("0 failed", result.output)
        self.assertNotIn(SECRET_VALUE, result.output)
        self.assertNotIn(KEY, result.output)

    def test_exit_nonzero_and_fix_line_on_failure(self) -> None:
        home = make_home(self.root, port=closed_port(), key=None)

        result = self.invoke(home, closed_port())

        self.assertEqual(result.exit_code, 1)
        self.assertIn("FAIL  daemon", result.output)
        self.assertIn("FAIL  master key", result.output)
        self.assertIn("fix: start it with `zhunt serve`", result.output)

    def test_warnings_alone_do_not_fail(self) -> None:
        with fake_daemon() as port:
            home = make_home(self.root, port=port)
            result = self.invoke(home, port, ZHUNT_ALLOW_PAID="1")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("WARN  ZHUNT_ALLOW_PAID", result.output)

    def test_doctor_does_not_write_anything(self) -> None:
        from tests.connect_support import snapshot

        with fake_daemon() as port:
            home = make_home(self.root, port=port)
            before = snapshot(home)
            self.invoke(home, port)

            self.assertEqual(snapshot(home), before)


if __name__ == "__main__":
    unittest.main()
