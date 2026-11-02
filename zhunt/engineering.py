"""Authenticated project/task workspaces and bounded local PTY sessions.

Terminal output is memory-only. Closing a window detaches; stopping the daemon
terminates its sessions. No saved command is executed automatically.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import select
import signal
import subprocess
import sys
import threading
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from zhunt.launch import build_plan
from zhunt.server import _authenticate_local_request

MAX_OUTPUT = 1024 * 1024
KINDS = ('shell', 'codex-zhunt', 'hermes-zhunt', 'codex', 'claude')


def git(directory: str, *args: str) -> str:
    result = subprocess.run(['git', '-C', directory, *args], capture_output=True, timeout=15)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors='replace')[:1000] or 'Git command failed')
    if len(result.stdout) > 2 * MAX_OUTPUT:
        return result.stdout[:2 * MAX_OUTPUT].decode(errors='replace') + '\n[output truncated]'
    return result.stdout.decode(errors='replace')


class ProjectInput(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class TaskInput(BaseModel):
    project: str
    name: str = Field(min_length=1, max_length=100)
    isolated: bool = False


class LaunchInput(BaseModel):
    task: str
    kind: str


class Input(BaseModel):
    data: str = Field(max_length=65536)


class Size(BaseModel):
    cols: int = Field(ge=2, le=500)
    rows: int = Field(ge=2, le=200)


class Session:
    def __init__(self, task: dict, kind: str, argv: list[str], env: dict):
        import pty
        self.id = uuid.uuid4().hex
        self.task = task['id']
        self.kind = kind
        self.lock = threading.Lock()
        self.output = bytearray()
        self.offset = 0
        self.master, slave = pty.openpty()
        self.closed = False
        os.set_blocking(self.master, False)
        try:
            self.resize(100, 28)
            # Set the controlling terminal in a fresh interpreter, never preexec_fn
            # in FastAPI's multithreaded process. The helper then execs the real CLI.
            helper = Path(__file__).with_name('terminal_child.py')
            self.process = subprocess.Popen(
                [sys.executable, str(helper), *argv], stdin=slave, stdout=slave,
                stderr=slave, cwd=task['path'], env=env, start_new_session=True,
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while True:
                if not select.select([self.master], [], [], 0.5)[0]:
                    continue
                try:
                    data = os.read(self.master, 16384)
                except BlockingIOError:
                    continue
                if not data:
                    break
                with self.lock:
                    self.output.extend(data)
                    self.offset += len(data)
                    if len(self.output) > MAX_OUTPUT:
                        del self.output[:-MAX_OUTPUT]
        except OSError:
            pass
        finally:
            self.process.wait()
            with self.lock:
                self.closed = True
                os.close(self.master)

    def summary(self):
        return {'id': self.id, 'task': self.task, 'kind': self.kind,
                'running': self.process.poll() is None, 'exit_code': self.process.poll()}

    def read(self, since: int):
        with self.lock:
            start = self.offset - len(self.output)
            reset = since < start or since > self.offset
            data = self.output[max(0, since - start) if not reset else 0:]
            return {**self.summary(), 'offset': self.offset, 'reset': reset,
                    'data': base64.b64encode(data).decode('ascii')}

    def write(self, data: str):
        with self.lock:
            if self.closed or self.process.poll() is not None:
                raise ValueError('This terminal has exited. Launch a new session.')
            payload = data.encode()
            while payload:
                if not select.select([], [self.master], [], 0.1)[1]:
                    raise ValueError('Terminal input buffer is full. Paste smaller chunks.')
                try:
                    written = os.write(self.master, payload[:1024])
                    payload = payload[written:]
                except BlockingIOError:
                    raise ValueError('Terminal input buffer is full. Try again.') from None

    def resize(self, cols: int, rows: int):
        import fcntl
        import struct
        import termios
        with self.lock:
            if not self.closed:
                fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    def stop(self):
        # Interactive shells put foreground/background jobs in separate process
        # groups. Stop their descendants too, before the shell can orphan them.
        with self.lock:
            if self.closed:
                return
            if self.process.poll() is None:
                listing = subprocess.run(['ps', '-axo', 'pid=,ppid='], capture_output=True, text=True, timeout=3)
                pairs = [tuple(map(int, line.split())) for line in listing.stdout.splitlines() if len(line.split()) == 2]
                descendants = {self.process.pid}
                while True:
                    found = {pid for pid, parent in pairs if parent in descendants}
                    expanded = descendants | found
                    if expanded == descendants:
                        break
                    descendants = expanded
                for pid in descendants - {self.process.pid}:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class Engineering:
    def __init__(self, directory: Path):
        self.directory = directory
        self.file = directory / 'engineering.json'
        self.lock = threading.RLock()
        self.sessions: dict[str, Session] = {}
        self.notice = None
        self.data = {'projects': [], 'tasks': []}
        if self.file.exists():
            try:
                loaded = json.loads(self.file.read_text())
                if not isinstance(loaded, dict) or not all(isinstance(loaded.get(k), list) for k in ('projects', 'tasks')):
                    raise ValueError('unexpected structure')
                self.data = loaded
            except (ValueError, OSError):
                # A damaged file must not stop routing. Keep it for recovery and start empty.
                aside = self.file.with_name(self.file.name + '.bad')
                try:
                    self.file.replace(aside)
                    self.notice = f'engineering.json was unreadable; it was kept as {aside.name} and projects start empty.'
                except OSError:
                    self.notice = 'engineering.json is unreadable and could not be moved aside; fix or remove it.'

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = self.file.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(self.data, out, indent=2)
        temp.replace(self.file)

    def snapshot(self):
        with self.lock:
            return {**self.data, 'notice': self.notice, 'sessions': [s.summary() for s in self.sessions.values()],
                    'supported': os.name == 'posix',
                    'agents': {k: bool(shutil.which(k)) for k in ('codex', 'claude', 'hermes')}}

    def project(self, directory: str):
        path = Path(directory).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError('Choose an existing project directory.')
        with self.lock:
            for project in self.data['projects']:
                if project['path'] == str(path):
                    return project
            project = {'id': uuid.uuid4().hex, 'name': path.name, 'path': str(path)}
            self.data['projects'].append(project)
            self.save()
            return project

    def task(self, id: str):
        for task in self.data['tasks']:
            if task['id'] == id:
                return task
        raise ValueError('Task not found.')

    def create_task(self, body: TaskInput):
        with self.lock:
            project = next((p for p in self.data['projects'] if p['id'] == body.project), None)
            if not project or not body.name.strip():
                raise ValueError('Choose a project and enter a task name.')
            id = uuid.uuid4().hex
            directory, branch = project['path'], None
            if body.isolated:
                branch = 'zhunt/' + id[:12]
                directory = str(self.directory / 'worktrees' / id)
                Path(directory).parent.mkdir(parents=True, exist_ok=True)
                git(project['path'], 'worktree', 'add', '-b', branch, directory, 'HEAD')
            task = {'id': id, 'project': body.project, 'name': body.name.strip(), 'path': directory, 'branch': branch}
            self.data['tasks'].append(task)
            self.save()
            return task

    def launch(self, body: LaunchInput, port: int, key: str):
        if os.name != 'posix':
            raise ValueError('Integrated terminals currently require macOS or Linux.')
        if body.kind not in KINDS:
            raise ValueError('Unknown terminal kind.')
        with self.lock:
            if sum(s.process.poll() is None for s in self.sessions.values()) >= 16:
                raise ValueError('Stop a terminal first (maximum 16 running sessions).')
            if len(self.sessions) >= 64:
                raise ValueError('Remove exited sessions first (maximum 64 saved sessions per daemon run).')
            task = self.task(body.task)
            env = dict(os.environ)
            # Shells and native agents must not inherit Zhunt's provider secrets.
            for name in list(env):
                if any(word in name for word in ('API_KEY', 'MASTER_KEY', 'TOKEN', 'SECRET', 'PASSWORD')):
                    env.pop(name)
            env.update(TERM='xterm-256color', COLORTERM='truecolor')
            if body.kind.endswith('-zhunt'):
                plan = build_plan(body.kind.removesuffix('-zhunt'), port=port, key=key)
                argv = list(plan.argv)
                env.update(plan.env)
            elif body.kind == 'shell':
                argv = [os.environ.get('SHELL', '/bin/sh'), '-l']
            else:
                argv = [body.kind]
            executable = shutil.which(argv[0])
            if not executable:
                raise ValueError(f'{argv[0]} is not installed or not on the daemon PATH.')
            argv[0] = executable
            session = Session(task, body.kind, argv, env)
            self.sessions[session.id] = session
            return session.summary()

    def session(self, id: str):
        if id not in self.sessions:
            raise ValueError('Terminal not found. The daemon may have restarted.')
        return self.sessions[id]

    def shutdown(self):
        for session in self.sessions.values():
            session.stop()
        for session in self.sessions.values():
            session.reader.join(timeout=2)


DISABLED_REASON = ('Terminals are off because the daemon is not bound to localhost. '
                   'Restart with the default --host 127.0.0.1 to use them.')


def mount_engineering(app, directory: Path, style_nonce: str, enabled: bool = True):
    router = APIRouter(prefix='/zhunt/api/engineering', dependencies=[Depends(_authenticate_local_request)])
    if not enabled:
        # A shell is arbitrary code execution as the user, so it is never offered
        # behind a network-reachable bind, where one key would be a remote shell.
        @router.get('')
        def disabled_state():
            return {'projects': [], 'tasks': [], 'sessions': [], 'supported': False,
                    'agents': {}, 'style_nonce': style_nonce, 'disabled_reason': DISABLED_REASON}

        @router.api_route('/{path:path}', methods=['GET', 'POST', 'DELETE'])
        def disabled(path: str):
            raise HTTPException(403, DISABLED_REASON)

        app.include_router(router)
        return
    service = Engineering(directory)
    app.state.engineering = service

    def perform(fn, *args):
        try:
            return fn(*args)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            raise HTTPException(400, str(error)) from error

    @router.get('')
    def state():
        return {**service.snapshot(), 'style_nonce': style_nonce}

    @router.post('/projects')
    def project(body: ProjectInput):
        return perform(service.project, body.path)

    @router.post('/tasks')
    def task(body: TaskInput):
        return perform(service.create_task, body)

    @router.post('/sessions')
    def launch(body: LaunchInput, request: Request):
        port = request.scope.get('server', ('127.0.0.1', 4000))[1]
        return perform(service.launch, body, port, app.state.zhunt_master_key)

    @router.get('/sessions/{id}')
    def output(id: str, since: int = 0):
        return perform(lambda: service.session(id).read(max(0, since)))

    @router.post('/sessions/{id}/input')
    def write(id: str, body: Input):
        perform(lambda: service.session(id).write(body.data))
        return {'ok': True}

    @router.post('/sessions/{id}/resize')
    def resize(id: str, body: Size):
        perform(lambda: service.session(id).resize(body.cols, body.rows))
        return {'ok': True}

    @router.post('/sessions/{id}/stop')
    def stop(id: str):
        perform(lambda: service.session(id).stop())
        return {'ok': True}

    @router.delete('/sessions/{id}')
    def remove(id: str):
        with service.lock:
            session = perform(service.session, id)
            if session.process.poll() is None:
                raise HTTPException(409, 'Stop the terminal before removing it.')
            del service.sessions[id]
        return {'ok': True}

    @router.get('/tasks/{id}/changes')
    def changes(id: str):
        directory = perform(service.task, id)['path']
        return perform(lambda: {'status': git(directory, 'status', '--short'),
                               'diff': git(directory, 'diff', '--no-ext-diff', '--no-textconv'),
                               'staged': git(directory, 'diff', '--cached', '--no-ext-diff', '--no-textconv')})

    app.include_router(router)
