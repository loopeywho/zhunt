"""Check installed wheel assets, persistence and a real terminal without provider calls."""
import base64
import os
from pathlib import Path
import tempfile
import time

import zhunt
from zhunt.engineering import Engineering, LaunchInput, TaskInput

root = Path(zhunt.__file__).parent
for asset in ('terminal_child.py', 'workspace/index.html',
              'workspace/js/tiles/engineering.js'):
    assert (root / asset).is_file(), asset
assert list((root / 'workspace').rglob('*xterm*.js')), 'Missing terminal assets'

with tempfile.TemporaryDirectory() as directory:
    home = Path(directory)
    os.environ['SHELL'] = '/bin/sh'
    engine = Engineering(home / 'state')
    project = engine.project(str(home))
    task = engine.create_task(TaskInput(project=project['id'], name='Install smoke'))
    try:
        info = engine.launch(LaunchInput(task=task['id'], kind='shell'), 4000, 'smoke-only')
        session = engine.session(info['id'])
        session.write("printf 'WHEEL_%s_OK\\n' INSTALL\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if b'WHEEL_INSTALL_OK' in base64.b64decode(session.read(0)['data']):
                break
            time.sleep(.05)
        else:
            raise AssertionError('Installed terminal did not execute the command')
    finally:
        engine.shutdown()
    restored = Engineering(home / 'state')
    assert restored.task(task['id']) == task
    assert restored.snapshot()['sessions'] == []
print('Installed wheel: assets, real PTY execution and task persistence passed')
