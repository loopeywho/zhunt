import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from zhunt.cli import app


class ServeCommandTests(unittest.TestCase):
    def test_serve_defaults_to_localhost(self) -> None:
        runner = CliRunner()

        with patch("zhunt.cli.run_proxy") as run_proxy:
            result = runner.invoke(app, ["serve"])

        self.assertEqual(result.exit_code, 0)
        run_proxy.assert_called_once_with(
            host="127.0.0.1",
            port=4000,
            registry_path=None,
        )


if __name__ == "__main__":
    unittest.main()
