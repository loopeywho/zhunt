import base64
import os
import subprocess
import time
from unittest.mock import AsyncMock

import pytest

from zhunt.engineering import Engineering, LaunchInput, TaskInput, MAX_OUTPUT
from tests.test_workspace_api import workspace, headers, ENV_NAMES
from tests.test_engine import ok


def wait_for(session, marker):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        data = base64.b64decode(session.read(0)['data'])
        if marker in data:
            return data
        time.sleep(.02)
    raise AssertionError(data)


def test_project_task_persistence_and_isolated_worktree(tmp_path):
    project = tmp_path / 'project'; project.mkdir()
    subprocess.run(['git', 'init', str(project)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(project), '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-m', 'initial'], check=True, capture_output=True)
    engine = Engineering(tmp_path / 'state')
    p = engine.project(str(project))
    assert engine.project(str(project)) == p
    task = engine.create_task(TaskInput(project=p['id'], name='Build feature', isolated=True))
    assert Path(task['path']).is_dir()
    assert task['path'] != str(project)
    restored = Engineering(tmp_path / 'state')
    assert restored.task(task['id']) == task
    assert restored.snapshot()['sessions'] == []
    assert engine.file.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        engine.create_task(TaskInput(project=p['id'], name=' '))


from pathlib import Path

@pytest.mark.skipif(os.name != 'posix', reason='POSIX PTYs')
def test_real_terminal_cwd_tty_resize_output_stop_and_replay(tmp_path, monkeypatch):
    monkeypatch.setenv('SHELL', '/bin/sh')
    monkeypatch.setenv('TEST_API_KEY', 'must-not-inherit')
    engine = Engineering(tmp_path / 'state')
    p = engine.project(str(tmp_path))
    task = engine.create_task(TaskInput(project=p['id'], name='Shell'))
    info = engine.launch(LaunchInput(task=task['id'], kind='shell'), 4000, 'secret')
    session = engine.session(info['id'])
    try:
        session.resize(111, 33)
        session.write("printf 'DIR='; pwd; stty size; test -t 0 && printf 'TTY_OK\\n'; test -z \"$TEST_API_KEY\" && printf 'NO_KEY\\n'\n")
        data = wait_for(session, b'NO_KEY\r\n')
        assert str(tmp_path).encode() in data
        assert b'33 111' in data and b'TTY_OK\r\n' in data
        # Snapshot offsets do not repeat bytes; old cursors explicitly signal truncation.
        end = session.read(0)['offset']
        assert session.read(end)['data'] == ''
        with session.lock:
            session.output = bytearray(b'x' * MAX_OUTPUT)
            session.offset = MAX_OUTPUT + 100
        assert session.read(0)['reset'] is True
        session.stop(); session.reader.join(timeout=3)
        assert not session.summary()['running']
        with pytest.raises(ValueError): session.write('anything')
    finally:
        engine.shutdown()


def test_engineering_api_auth_validation_and_shutdown(tmp_path):
    with workspace(AsyncMock(return_value=ok()), env={}) as (client, hook):
        assert client.get('/zhunt/api/engineering').status_code == 401
        assert client.post('/zhunt/api/engineering/projects', json={'path': str(tmp_path)}).status_code == 401
        p = client.post('/zhunt/api/engineering/projects', headers=headers(), json={'path': str(tmp_path)}).json()
        t = client.post('/zhunt/api/engineering/tasks', headers=headers(), json={'project': p['id'], 'name': 'Test'}).json()
        assert client.post('/zhunt/api/engineering/sessions', headers=headers(), json={'task': t['id'], 'kind': 'arbitrary command'}).status_code == 400
        assert client.post('/zhunt/api/engineering/sessions/missing/resize', headers=headers(), json={'cols': 999999, 'rows': 20}).status_code == 422
        state = client.get('/zhunt/api/engineering', headers=headers()).json()
        assert state['tasks'][0]['id'] == t['id']
        assert 'style_nonce' in state
        assert 'unsafe-inline' not in client.get('/workspace/').headers['content-security-policy']


def test_routed_launch_uses_existing_router_plan(tmp_path, monkeypatch):
    import zhunt.engineering as module
    captured = {}
    class FakeSession:
        id = 'fake'
        def __init__(self, task, kind, argv, env): captured.update(argv=argv, env=env)
        def summary(self): return {'id': self.id}
    monkeypatch.setattr(module, 'Session', FakeSession)
    monkeypatch.setattr(module.shutil, 'which', lambda value: '/bin/' + value)
    engine = Engineering(tmp_path / 'state')
    p = engine.project(str(tmp_path))
    t = engine.create_task(TaskInput(project=p['id'], name='Coding'))
    engine.launch(LaunchInput(task=t['id'], kind='codex-zhunt'), 4567, 'local-master-key')
    assert 'model="zhunt-auto"' in captured['argv']
    assert 'model_providers.zhunt.base_url="http://127.0.0.1:4567/v1"' in captured['argv']
    assert captured['env']['ZHUNT_API_KEY'] == 'local-master-key'
    assert 'local-master-key' not in ' '.join(captured['argv'])

@pytest.mark.skipif(os.name != 'posix', reason='POSIX PTYs')
def test_stop_terminates_background_job_and_daemon_shutdown(tmp_path, monkeypatch):
    monkeypatch.setenv('SHELL', '/bin/sh')
    with workspace(AsyncMock(return_value=ok()), env={}) as (client, hook):
        p = client.post('/zhunt/api/engineering/projects', headers=headers(), json={'path': str(tmp_path)}).json()
        t = client.post('/zhunt/api/engineering/tasks', headers=headers(), json={'project': p['id'], 'name': 'Cleanup'}).json()
        info = client.post('/zhunt/api/engineering/sessions', headers=headers(), json={'task': t['id'], 'kind': 'shell'}).json()
        route = '/zhunt/api/engineering/sessions/' + info['id']
        assert client.delete(route, headers=headers()).status_code == 409
        client.post(route + '/input', headers=headers(), json={'data': "sleep 120 & echo JOB=$!\n"})
        deadline = time.monotonic() + 5
        import re
        match = None
        while time.monotonic() < deadline and not match:
            result = client.get(route, headers=headers()).json()
            match = re.search(rb'JOB=(\d+)\r?\n', base64.b64decode(result['data']))
            time.sleep(.02)
        assert match
        child = int(match[1])
    # TestClient leaving the lifespan shuts down the PTY and its background sleep.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = subprocess.run(['ps', '-p', str(child), '-o', 'stat='], capture_output=True, text=True).stdout.strip()
        if not status or status.startswith('Z'):
            break
        time.sleep(.02)
    assert not status or status.startswith('Z')
