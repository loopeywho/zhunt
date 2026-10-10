import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from typer.testing import CliRunner

from tests.connect_support import KEY, clean_environ, make_home, snapshot
from zhunt.cli import app


class OpenCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def invoke(self, args, home, opened=None):
        env = clean_environ(home)
        with patch.dict(os.environ, env, clear=True), patch(
            "zhunt.connect_cli.webbrowser.open", return_value=True
        ) as browser:
            result = CliRunner().invoke(app, ["open", *args])
        if opened is not None:
            opened.extend(call.args[0] for call in browser.call_args_list)
        return result

    def test_print_shows_url_with_key_in_fragment_only(self) -> None:
        home = make_home(self.root, port=4123)
        opened: list[str] = []

        result = self.invoke(["--print"], home, opened)

        self.assertEqual(result.exit_code, 0, result.output)
        url = result.output.strip().splitlines()[-1]
        parsed = urlparse(url)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ("http", "127.0.0.1:4123", "/workspace/"))
        self.assertEqual(parsed.fragment, f"key={KEY}")
        self.assertEqual(parsed.query, "")
        self.assertNotIn(KEY, parsed.path + parsed.query)
        self.assertEqual(opened, [])

    def test_default_opens_the_browser_and_does_not_echo_the_key(self) -> None:
        home = make_home(self.root, port=4123)
        opened: list[str] = []

        result = self.invoke([], home, opened)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(opened, [f"http://127.0.0.1:4123/workspace/#key={KEY}"])
        self.assertNotIn(KEY, result.output)
        self.assertIn("fragment", result.output)

    def test_port_option_overrides_saved_port(self) -> None:
        home = make_home(self.root, port=4123)

        result = self.invoke(["--print", "--port", "5001"], home)

        self.assertIn("127.0.0.1:5001/workspace/#key=", result.output)

    def test_falls_back_to_default_port(self) -> None:
        home = make_home(self.root, port=None)

        result = self.invoke(["--print"], home)

        self.assertIn("127.0.0.1:4000/workspace/#key=", result.output)

    def test_missing_key_fails_and_creates_nothing(self) -> None:
        home = make_home(self.root, port=4123, key=None)
        before = snapshot(home)
        opened: list[str] = []

        result = self.invoke([], home, opened)

        self.assertEqual(result.exit_code, 1)
        self.assertIn("zhunt serve --free-tier", result.output)
        self.assertEqual(opened, [])
        self.assertEqual(snapshot(home), before)

    def test_browser_failure_is_reported(self) -> None:
        home = make_home(self.root, port=4123)
        env = clean_environ(home)
        with patch.dict(os.environ, env, clear=True), patch(
            "zhunt.connect_cli.webbrowser.open", return_value=False
        ):
            result = CliRunner().invoke(app, ["open"])

        self.assertEqual(result.exit_code, 1)
        self.assertIn("--print", result.output)
        self.assertNotIn(KEY, result.output)

    def test_help_explains_the_fragment(self) -> None:
        result = CliRunner().invoke(app, ["open", "--help"])

        self.assertEqual(result.exit_code, 0)
        flat = " ".join(result.output.split())
        self.assertIn("fragment", flat)
        self.assertIn("never send to a server", flat)

    def test_url_query_parser_sees_no_key(self) -> None:
        home = make_home(self.root, port=4123)

        url = self.invoke(["--print"], home).output.strip()

        self.assertEqual(parse_qs(urlparse(url).query), {})


if __name__ == "__main__":
    unittest.main()
