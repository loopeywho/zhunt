"""Dispatch regressions; real frozen execution is scripts/smoke_terminal.py."""
import importlib.util
import os
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from zhunt import terminal_child


@pytest.fixture(params=['linux', 'macos', 'windows'])
def packaged_entrypoint(request):
    # Read the actual build recipe so a platform-specific entrypoint cannot
    # silently fall outside the dispatch regressions again.
    import re
    root = Path(__file__).parents[1]
    suffix = 'ps1' if request.param == 'windows' else 'sh'
    recipe = (root / 'packaging' / request.param / f'build.{suffix}').read_text()
    paths = re.findall(r'packaging[/\\][a-z]+[/\\]entrypoint\.py', recipe)
    assert len(paths) == 1, f'Expected one entrypoint in {request.param} build'
    return root / paths[0].replace('\\', '/')


def entrypoint(path):
    spec = importlib.util.spec_from_file_location('desktop_entrypoint', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('frozen,args,helper', [
    (True, ['--zhunt-terminal-child', '/bin/sh', '-l'], True),
    (False, ['--zhunt-terminal-child', '/bin/sh'], False),
    (True, ['desktop', '--zhunt-terminal-child'], False),
    (True, ['--zhunt-terminal-child-extra', '/bin/sh'], False),
    (True, ['--help'], False),
    (True, [], False),
])
def test_exact_early_dispatch(monkeypatch, packaged_entrypoint, frozen, args, helper):
    import zhunt.cli
    app, child = Mock(), Mock()
    monkeypatch.setattr(sys, 'frozen', frozen, raising=False)
    monkeypatch.setattr(sys, 'argv', ['zhunt', *args])
    monkeypatch.setattr(zhunt.cli, 'app', app)
    monkeypatch.setattr(terminal_child, 'main', child)
    entrypoint(packaged_entrypoint).main()
    if helper:
        child.assert_called_once_with(args[1:])
        app.assert_not_called()
    else:
        app.assert_called_once_with()
        child.assert_not_called()


def test_packaged_main_dispatches_before_cli_import(monkeypatch, packaged_entrypoint):
    import builtins
    import runpy
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name not in ('zhunt.cli', 'zhunt.server'), 'helper imported CLI/server'
        return original_import(name, *args, **kwargs)

    child = Mock()
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'argv', ['zhunt', terminal_child.FROZEN_TERMINAL_FLAG, '/path with spaces/sh', '-l'])
    monkeypatch.setattr(terminal_child, 'main', child)
    monkeypatch.setattr(builtins, '__import__', guarded_import)
    runpy.run_path(str(packaged_entrypoint), run_name='__main__')
    child.assert_called_once_with(['/path with spaces/sh', '-l'])


@pytest.mark.skipif(os.name != 'posix', reason='POSIX PTYs')
@pytest.mark.parametrize('frozen', [False, True])
def test_session_spawn_vector_and_fd_cleanup(tmp_path, monkeypatch, frozen):
    import zhunt.engineering as eng
    monkeypatch.setattr(sys, 'frozen', frozen, raising=False)
    calls = []
    def fail(argv, **kw):
        calls.append((argv, kw))
        raise OSError('spawn probe')
    monkeypatch.setattr(eng.subprocess, 'Popen', fail)
    with pytest.raises(OSError, match='spawn probe'):
        eng.Session({'id': 'test', 'path': str(tmp_path)}, 'shell', ['/bin/sh', '-l'], {'TERM': 'xterm'})
    argv, kw = calls[0]
    expected = terminal_child.FROZEN_TERMINAL_FLAG if frozen else str(Path(eng.__file__).with_name('terminal_child.py'))
    assert argv == [sys.executable, expected, '/bin/sh', '-l']
    assert kw['start_new_session'] is True and 'preexec_fn' not in kw
    assert kw['cwd'] == str(tmp_path) and kw['env'] == {'TERM': 'xterm'}
    assert kw['stdin'] == kw['stdout'] == kw['stderr']
    with pytest.raises(OSError):
        os.fstat(kw['stdin'])


def test_helper_requires_command():
    with pytest.raises(SystemExit):
        terminal_child.main([])


@pytest.mark.skipif(os.name != 'posix', reason='POSIX helper')
def test_helper_fails_closed_without_controlling_tty(monkeypatch):
    import fcntl
    execute = Mock()
    monkeypatch.setattr(os, 'execvpe', execute)
    monkeypatch.setattr(fcntl, 'ioctl', Mock(side_effect=OSError('not a tty')))
    with pytest.raises(OSError, match='not a tty'):
        terminal_child.main(['/bin/sh'])
    execute.assert_not_called()


@pytest.mark.skipif(os.name != 'posix', reason='POSIX helper')
@pytest.mark.parametrize('original', [None, '/system/libs', ''])
def test_frozen_helper_restores_loader_path_and_preserves_argv(monkeypatch, original):
    import fcntl
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(fcntl, 'ioctl', Mock())
    monkeypatch.setenv('LD_LIBRARY_PATH', '/bundle')
    monkeypatch.delenv('LD_LIBRARY_PATH_ORIG', raising=False)
    if original is not None:
        monkeypatch.setenv('LD_LIBRARY_PATH_ORIG', original)
    execute = Mock()
    monkeypatch.setattr(os, 'execvpe', execute)
    argv = ['/path with spaces/tool', 'literal; argument', '--flag']
    terminal_child.main(argv)
    executable, actual, env = execute.call_args.args
    assert executable == argv[0] and actual == argv
    assert env.get('LD_LIBRARY_PATH') == original
    assert 'LD_LIBRARY_PATH_ORIG' not in env
