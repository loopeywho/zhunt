"""Give a child session its controlling TTY before executing a terminal program.

Imported by the frozen entry point only for its private helper dispatch. Keep
POSIX imports lazy so collecting this module does not break Windows startup.
"""
import os
import sys

FROZEN_TERMINAL_FLAG = '--zhunt-terminal-child'


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if os.name != 'posix' or not argv:
        raise SystemExit('terminal helper requires POSIX and a command')
    import fcntl
    import termios

    # Must fail before exec if not attached to a PTY in a new session.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    env = dict(os.environ)
    if getattr(sys, 'frozen', False):
        # PyInstaller prepends its bundled libraries on Linux. External programs
        # must use the original loader path, not our private Python libraries.
        if 'LD_LIBRARY_PATH_ORIG' in env:
            env['LD_LIBRARY_PATH'] = env.pop('LD_LIBRARY_PATH_ORIG')
        else:
            env.pop('LD_LIBRARY_PATH', None)
    os.execvpe(argv[0], argv, env)


if __name__ == '__main__':
    main()
