import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from typer.testing import CliRunner

from tests.connect_support import (
    KEY,
    clean_environ,
    closed_port,
    fake_daemon,
    make_home,
    read_stub_output,
    snapshot,
    write_stub,
)
from zhunt.cli import app
from zhunt.launch import LaunchError, build_plan


class PlanTests(unittest.TestCase):
    def test_claude_plan_pins_endpoint_key_and_every_model_slot(self) -> None:
        plan = build_plan("claude", port=4321, key=KEY, extra_args=("-p", "hi"))

        self.assertEqual(plan.argv, ("claude", "-p", "hi"))
        self.assertEqual(plan.env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:4321")
        self.assertEqual(plan.env["ANTHROPIC_AUTH_TOKEN"], KEY)
        for name in (
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ):
            self.assertEqual(plan.env[name], "zhunt-auto", name)
        self.assertIn("ANTHROPIC_API_KEY", plan.unset)
        self.assertIn("CLAUDE_CODE_USE_BEDROCK", plan.unset)

    def test_codex_plan_uses_config_overrides_that_name_an_env_var(self) -> None:
        plan = build_plan("codex", port=4321, key=KEY, extra_args=("exec", "hi"))

        pairs = [plan.argv[i + 1] for i, part in enumerate(plan.argv) if part == "-c"]
        self.assertIn('model_provider="zhunt"', pairs)
        self.assertIn('model="zhunt-auto"', pairs)
        self.assertIn('model_providers.zhunt.base_url="http://127.0.0.1:4321/v1"', pairs)
        self.assertIn('model_providers.zhunt.env_key="ZHUNT_API_KEY"', pairs)
        self.assertIn('model_providers.zhunt.wire_api="responses"', pairs)
        self.assertEqual(plan.argv[-2:], ("exec", "hi"))
        self.assertEqual(plan.env, {"ZHUNT_API_KEY": KEY})

    def test_hermes_plan_selects_custom_provider_and_pairs_key_with_endpoint(self) -> None:
        plan = build_plan("hermes", port=4321, key=KEY, extra_args=("chat",))

        self.assertEqual(
            plan.argv,
            ("hermes", "--provider", "custom", "-m", "zhunt-auto", "chat"),
        )
        endpoint = "http://127.0.0.1:4321/v1"
        # Hermes only accepts OPENAI_API_KEY for an endpoint equal to OPENAI_BASE_URL.
        self.assertEqual(plan.env["CUSTOM_BASE_URL"], endpoint)
        self.assertEqual(plan.env["OPENAI_BASE_URL"], endpoint)
        self.assertEqual(plan.env["OPENAI_API_KEY"], KEY)

    def test_no_plan_puts_the_key_on_the_command_line(self) -> None:
        for name in ("claude", "codex", "hermes"):
            plan = build_plan(name, port=4000, key=KEY)
            self.assertFalse(any(KEY in part for part in plan.argv), name)

    def test_unknown_app_is_rejected(self) -> None:
        with self.assertRaises(LaunchError):
            build_plan("emacs", port=4000, key=KEY)


class LaunchCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        for name in ("claude", "codex", "hermes"):
            write_stub(self.stubs, name)
        self.out = self.root / "stub-out.json"
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()
        self.runner = CliRunner()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def invoke(self, args, home, **env_extra):
        env = clean_environ(home, self.stubs, STUB_OUT=str(self.out), **env_extra)
        previous = os.getcwd()
        os.chdir(self.cwd)
        try:
            with patch.dict(os.environ, env, clear=True):
                return self.runner.invoke(app, args)
        finally:
            os.chdir(previous)

    def test_each_app_receives_endpoint_and_key_only_in_its_environment(self) -> None:
        expectations = {
            "claude": ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"),
            "codex": ("ZHUNT_API_KEY", None),
            "hermes": ("OPENAI_API_KEY", "CUSTOM_BASE_URL"),
        }
        with fake_daemon() as port:
            home = make_home(self.root, port=port)
            for name, (key_var, url_var) in expectations.items():
                with self.subTest(app=name):
                    self.out.unlink(missing_ok=True)
                    result = self.invoke(["launch", name], home)

                    self.assertEqual(result.exit_code, 0, result.output)
                    seen = read_stub_output(self.out)
                    self.assertEqual(seen["env"][key_var], KEY)
                    if url_var:
                        self.assertIn(f"127.0.0.1:{port}", seen["env"][url_var])
                    self.assertFalse(any(KEY in arg for arg in seen["argv"]))
                    self.assertNotIn(KEY, result.output)

    def test_claude_drops_conflicting_ambient_credentials(self) -> None:
        with fake_daemon() as port:
            home = make_home(self.root, port=port)
            result = self.invoke(
                ["launch", "claude"],
                home,
                ANTHROPIC_API_KEY="sk-ant-real-key",
                CLAUDE_CODE_USE_BEDROCK="1",
            )

        self.assertEqual(result.exit_code, 0, result.output)
        env = read_stub_output(self.out)["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)

    def test_codex_argv_carries_overrides_and_extra_args(self) -> None:
        with fake_daemon() as port:
            home = make_home(self.root, port=port)
            result = self.invoke(["launch", "codex", "--", "exec", "--json", "hello"], home)

        self.assertEqual(result.exit_code, 0, result.output)
        argv = read_stub_output(self.out)["argv"]
        self.assertIn(f'model_providers.zhunt.base_url="http://127.0.0.1:{port}/v1"', argv)
        self.assertEqual(argv[-3:], ["exec", "--json", "hello"])

    def test_extra_args_pass_through_with_or_without_separator(self) -> None:
        with fake_daemon() as port:
            home = make_home(self.root, port=port)
            self.invoke(["launch", "claude", "--", "-p", "hello", "--dry-run"], home)
            self.assertEqual(read_stub_output(self.out)["argv"], ["-p", "hello", "--dry-run"])
            self.invoke(["launch", "claude", "--model", "x"], home)
            self.assertEqual(read_stub_output(self.out)["argv"], ["--model", "x"])

    def test_launch_never_creates_or_modifies_any_file(self) -> None:
        with fake_daemon() as port:
            home = make_home(self.root, port=port)
            # Pre-existing app configs that v1 would have rewritten.
            (home / ".claude").mkdir()
            (home / ".claude" / "settings.json").write_text('{"theme": "dark"}\n')
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text('model = "gpt-x"\n')
            (home / ".hermes").mkdir()
            (home / ".hermes" / "config.yaml").write_text("model:\n  default: x\n")
            before = snapshot(home, self.cwd)
            for name in ("claude", "codex", "hermes"):
                result = self.invoke(["launch", name], home)
                self.assertEqual(result.exit_code, 0, result.output)
                result = self.invoke(["launch", name, "--dry-run"], home)
                self.assertEqual(result.exit_code, 0, result.output)

            self.assertEqual(snapshot(home, self.cwd), before)

    def test_dry_run_hides_key_and_starts_nothing(self) -> None:
        with fake_daemon() as port:
            home = make_home(self.root, port=port)
            for name in ("claude", "codex", "hermes"):
                with self.subTest(app=name):
                    result = self.invoke(["launch", name, "--dry-run"], home)

                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertNotIn(KEY, result.output)
                    self.assertIn("***", result.output)
                    self.assertIn(f"127.0.0.1:{port}", result.output)
                    self.assertIn("reachable", result.output)
                    self.assertFalse(self.out.exists())

    def test_dry_run_works_without_key_or_daemon(self) -> None:
        home = make_home(self.root, port=closed_port(), key=None)

        result = self.invoke(["launch", "hermes", "--dry-run"], home)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("NOT reachable", result.output)

    def test_child_exit_code_passes_through(self) -> None:
        with fake_daemon() as port:
            home = make_home(self.root, port=port)
            result = self.invoke(["launch", "claude"], home, STUB_EXIT="7")

        self.assertEqual(result.exit_code, 7)

    def test_child_killed_by_signal_reports_shell_style_status(self) -> None:
        with fake_daemon() as port:
            home = make_home(self.root, port=port)
            result = self.invoke(["launch", "claude"], home, STUB_MODE="selfkill")

        self.assertEqual(result.exit_code, 128 + signal.SIGKILL)

    def test_unreachable_daemon_fails_early_with_start_command(self) -> None:
        home = make_home(self.root, port=closed_port())

        result = self.invoke(["launch", "claude"], home)

        self.assertEqual(result.exit_code, 1)
        self.assertIn("zhunt serve", result.output)
        self.assertFalse(self.out.exists())
        self.assertNotIn(KEY, result.output)

    def test_missing_master_key_fails_without_creating_one(self) -> None:
        with fake_daemon() as port:
            home = make_home(self.root, port=port, key=None)
            before = snapshot(home)

            result = self.invoke(["launch", "claude"], home)

            self.assertEqual(result.exit_code, 1)
            self.assertIn("master key", result.output)
            self.assertEqual(snapshot(home), before)
            self.assertFalse(self.out.exists())

    def test_app_not_installed_exits_127(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        with fake_daemon() as port:
            home = make_home(self.root, port=port)
            result = self.invoke(["launch", "codex"], home, PATH=str(empty))

        self.assertEqual(result.exit_code, 127)
        self.assertIn("not found", result.output)

    def test_apps_without_a_config_free_route_print_manual_steps_and_fail(self) -> None:
        home = make_home(self.root, port=4000)
        for name in ("cursor", "vscode"):
            with self.subTest(app=name):
                result = self.invoke(["launch", name], home)

                self.assertEqual(result.exit_code, 2)
                self.assertIn("Manual steps", result.output)
                self.assertIn("127.0.0.1:4000", result.output)
                self.assertNotIn(KEY, result.output)
                self.assertFalse(self.out.exists())

    def test_unknown_app_is_a_usage_error(self) -> None:
        home = make_home(self.root, port=4000)

        result = self.invoke(["launch", "emacs"], home)

        self.assertNotEqual(result.exit_code, 0)

    def test_v1_settings_that_would_override_the_endpoint_are_flagged(self) -> None:
        with fake_daemon() as port:
            home = make_home(self.root, port=port)
            (home / ".claude").mkdir()
            (home / ".claude" / "settings.json").write_text(
                json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1"}})
            )
            (home / ".hermes").mkdir()
            (home / ".hermes" / ".env").write_text("OPENAI_API_KEY=sk-someone-elses\n")

            claude = self.invoke(["launch", "claude", "--dry-run"], home)
            hermes = self.invoke(["launch", "hermes", "--dry-run"], home)

        self.assertIn("ANTHROPIC_BASE_URL", claude.output)
        self.assertIn("will ignore", claude.output)
        self.assertIn("OPENAI_API_KEY", hermes.output)
        self.assertNotIn("sk-someone-elses", hermes.output)


class SignalForwardingTests(unittest.TestCase):
    """Runs the real CLI in a subprocess so signals hit a real zhunt process."""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        write_stub(self.stubs, "hermes")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_and_signal(self, signum: int) -> tuple[int, str]:
        ready = self.root / "ready"
        seen = self.root / "signal"
        with fake_daemon() as port:
            home = make_home(self.root, port=port)
            env = clean_environ(
                home,
                self.stubs,
                STUB_MODE="wait",
                STUB_READY_FILE=str(ready),
                STUB_SIGNAL_FILE=str(seen),
            )
            process = subprocess.Popen(
                [sys.executable, "-c", "from zhunt.cli import app; app()", "launch", "hermes"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.time() + 60
                while not ready.exists() and time.time() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), "stub never started")
                process.send_signal(signum)
                code = process.wait(timeout=30)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
        return code, seen.read_text() if seen.exists() else ""

    def test_sigterm_reaches_the_child_and_its_exit_code_comes_back(self) -> None:
        code, received = self.run_and_signal(signal.SIGTERM)

        self.assertEqual(received, "TERM")
        self.assertEqual(code, 42)

    def test_sigint_is_forwarded_when_there_is_no_terminal(self) -> None:
        code, received = self.run_and_signal(signal.SIGINT)

        self.assertEqual(received, "INT")
        self.assertEqual(code, 43)


if __name__ == "__main__":
    unittest.main()
