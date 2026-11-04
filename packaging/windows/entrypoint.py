"""Shared PyInstaller entry point for the desktop distributions."""
import sys


def main():
    # Dispatch before importing the CLI/server. A frozen executable is not a
    # Python interpreter and cannot run terminal_child.py as a script argument.
    from zhunt.terminal_child import FROZEN_TERMINAL_FLAG
    if getattr(sys, 'frozen', False) and sys.argv[1:2] == [FROZEN_TERMINAL_FLAG]:
        from zhunt.terminal_child import main as terminal_main
        terminal_main(sys.argv[2:])
        return
    from zhunt.cli import app
    app()


if __name__ == "__main__":
    main()
