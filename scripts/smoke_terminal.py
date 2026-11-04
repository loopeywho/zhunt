"""Actual authenticated Engineering API/PTY checks for smoke_desktop --terminal."""
import base64
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def run_terminal(base, key, home):
    root = '/zhunt/api/engineering'
    def api(method, path, body=None, authenticated=True):
        headers = {'Authorization': 'Bearer ' + key} if authenticated else {}
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body).encode()
        with urllib.request.urlopen(urllib.request.Request(base + root + path, data=data, headers=headers, method=method), timeout=5) as response:
            return json.load(response)

    for method, path, body in [('GET', '', None), ('POST', '/sessions', {'task': 'absent', 'kind': 'shell'})]:
        try:
            api(method, path, body, authenticated=False)
        except urllib.error.HTTPError as error:
            assert error.code == 401
        else:
            raise AssertionError('Engineering API allowed unauthenticated access')
    assert api('GET', '')['supported'] is True
    project_dir = home / 'project with spaces'
    project_dir.mkdir()
    project = api('POST', '/projects', {'path': str(project_dir)})
    task = api('POST', '/tasks', {'project': project['id'], 'name': 'Frozen PTY smoke'})
    session = api('POST', '/sessions', {'task': task['id'], 'kind': 'shell'})
    route = '/sessions/' + session['id']
    transcript = b''
    def wait_for(marker):
        nonlocal transcript
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = api('GET', route)
            transcript = base64.b64decode(state['data'])
            if marker in transcript:
                return state
            time.sleep(.05)
        raise AssertionError(repr(transcript))
    def send(command):
        return api('POST', route + '/input', {'data': command})
    try:
        # Disable input echo, then use a nonce file as independent proof of exec.
        nonce = uuid.uuid4().hex
        api('POST', route + '/resize', {'cols': 117, 'rows': 39})
        send('stty -echo\n')
        send("printf '%s' " + shlex.quote(nonce) + " > executed.txt; printf 'CWD='; pwd; stty size; test -t 0 && test -t 1 && test -t 2 && printf 'TTY_OK\\n'; ( : </dev/tty ) && printf 'CONTROL_TTY_OK\\n'; printf 'PID=%s\\n' $$; printf 'READY_%s\\n' " + nonce + '\n')
        wait_for(('READY_' + nonce + '\r\n').encode())
        assert (project_dir / 'executed.txt').read_text() == nonce
        assert ('CWD=' + str(project_dir) + '\r\n').encode() in transcript
        assert b'39 117\r\n' in transcript
        assert b'TTY_OK\r\n' in transcript and b'CONTROL_TTY_OK\r\n' in transcript
        match = re.search(rb'PID=(\d+)\r\n', transcript)
        assert match is not None
        shell_pid = int(match[1])
        send('sleep 60\n')
        time.sleep(.3)
        send('\x03')
        send("printf 'INTERRUPT_%s\\n' " + nonce + '\n')
        wait_for(('INTERRUPT_' + nonce + '\r\n').encode())
        send("sleep 120 & printf 'JOB=%s\\n' $!; printf 'JOB_READY_%s\\n' " + nonce + '\n')
        wait_for(('JOB_READY_' + nonce + '\r\n').encode())
        match = re.search(rb'JOB=(\d+)\r\n', transcript)
        assert match is not None
        job = int(match[1])
        api('POST', route + '/stop')
        state, statuses = {'running': True}, ['running']
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state = api('GET', route)
            statuses = [subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True, check=False).stdout.strip() for pid in (shell_pid, job)]
            if not state['running'] and all(not s or s.startswith('Z') for s in statuses):
                break
            time.sleep(.05)
        assert not state['running'] and all(not s or s.startswith('Z') for s in statuses)
        api('DELETE', route)
        return {'authenticated_api': True, 'unauthenticated_rejected': True, 'real_shell_file_nonce': nonce,
                'cwd_with_spaces': str(project_dir), 'controlling_tty': True, 'resize': [117, 39],
                'interrupt': True, 'stop_shell_and_background_job': True, 'shell_pid': shell_pid,
                'background_pid': job, 'exit_code': state['exit_code'], 'transcript': transcript.decode(errors='replace')}
    finally:
        try:
            api('POST', route + '/stop')
        except urllib.error.HTTPError as error:
            if error.code != 400:
                raise
