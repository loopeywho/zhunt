"""PyInstaller entry point for the Zhunt Linux distribution."""

import sys


def main():
    # Frozen executables are not Python interpreters. Dispatch the private PTY
    # helper before importing CLI/server code, as in the Mac/Windows entrypoint.
    from zhunt.terminal_child import FROZEN_TERMINAL_FLAG
    if getattr(sys, 'frozen', False) and sys.argv[1:2] == [FROZEN_TERMINAL_FLAG]:
        from zhunt.terminal_child import main as terminal_main
        terminal_main(sys.argv[2:])
        return
    from zhunt.cli import app
    app()


if __name__ == "__main__":
    main()
