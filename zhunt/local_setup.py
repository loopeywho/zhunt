"""Set up a local model server (Zhunt Plus on InferSSD) for ``zhunt local``.

Every step works on this computer only. Nothing is downloaded: the InferSSD
source must already be checked out locally (it is cloned from that folder), the
Zhunt Plus patches and launcher come from a Zhunt Plus folder, and the model must
already be a complete GGUF on disk. Model licences are not checked here.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

# The InferSSD revision the Zhunt Plus patches were made against.
PINNED_REVISION = "368df4f1a4d49c564ded47240b45fbd0dca8b084"
# The patches applied on top, in order, from <zhunt-plus>/engine/inferssd-patches.
PATCH_PREFIXES = ("0001-", "0002-", "0003-")
STATE_DIR = Path.home() / ".zhunt" / "local"
MARKER = ".zhunt-engine.json"
MIN_RAM_GIB = 16  # the target buyer's machine; only 24 GB has been measured
_SHARD = re.compile(r"(.+)-([0-9]{5})-of-([0-9]{5})\.gguf")

Runner = Callable[..., subprocess.CompletedProcess]


class SetupError(RuntimeError):
    """A setup step cannot go ahead; the message says why and what to do."""


# -- hardware ------------------------------------------------------------------


@dataclass(frozen=True)
class Hardware:
    apple_silicon: bool
    ram_gib: float
    free_disk_gb: float
    disk_path: str


def _sysctl(key: str) -> str:
    try:
        return subprocess.run(
            ["sysctl", "-n", key], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def hardware(disk_path: Path) -> Hardware:
    ram = int(_sysctl("hw.memsize") or 0) if sys.platform == "darwin" else 0
    path = disk_path
    while not path.exists() and path != path.parent:
        path = path.parent
    return Hardware(
        apple_silicon=sys.platform == "darwin" and platform.machine() == "arm64",
        ram_gib=round(ram / 2**30, 1),
        free_disk_gb=round(shutil.disk_usage(path).free / 1e9, 1),
        disk_path=str(path),
    )


def hardware_problems(hw: Hardware) -> tuple[list[str], list[str]]:
    """(blocking problems, warnings)."""

    errors, warnings = [], []
    if not hw.apple_silicon:
        errors.append("this needs an Apple Silicon Mac (the only build profile tested)")
    if hw.ram_gib and hw.ram_gib < MIN_RAM_GIB:
        warnings.append(f"{hw.ram_gib} GiB of RAM is below the {MIN_RAM_GIB} GiB target")
    if hw.free_disk_gb < 10:
        warnings.append(
            f"only {hw.free_disk_gb} GB free on {hw.disk_path}; the engine build needs a few GB"
        )
    return errors, warnings


def _no_cache(fd: int) -> None:
    if sys.platform == "darwin":
        import fcntl

        fcntl.fcntl(fd, 48, 1)  # F_NOCACHE: read from the SSD, not the page cache


def ssd_read_benchmark(
    path: Path, *, seconds: float = 6.0, chunk_mb: int = 32, threads: int = 4
) -> float:
    """Random reads of ``path`` with the page cache bypassed, in GB/s.

    Expert streaming does random reads of expert-sized blocks, so that is what is
    timed. Read-only: run it on an existing large file (the model itself), never
    on a fresh file, which would still be partly cached.
    """

    size = path.stat().st_size
    chunk = chunk_mb << 20
    if size <= chunk:
        raise SetupError(f"{path.name} is too small to benchmark ({size} bytes)")
    total = [0]
    lock = threading.Lock()
    deadline = time.monotonic() + seconds

    def reader() -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            _no_cache(fd)
            read = 0
            while time.monotonic() < deadline:
                offset = random.randrange(0, size - chunk) & ~4095
                read += len(os.pread(fd, chunk, offset))
        finally:
            os.close(fd)
        with lock:
            total[0] += read

    start = time.monotonic()
    workers = [threading.Thread(target=reader) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    return round(total[0] / 1e9 / max(time.monotonic() - start, 1e-9), 2)


# -- models --------------------------------------------------------------------


def complete_model(path: Path) -> Path:
    """``path`` if it is a whole GGUF or the first shard of a complete set."""

    path = path.expanduser().resolve()
    if not path.is_file() or path.suffix != ".gguf":
        raise SetupError(f"{path} is not a .gguf file")
    match = _SHARD.fullmatch(path.name)
    if match:
        prefix, number, count = match.groups()
        if int(number) != 1:
            raise SetupError(f"{path.name} is not the first shard; choose -00001-of-{count}")
        for index in range(1, int(count) + 1):
            shard = path.with_name(f"{prefix}-{index:05d}-of-{count}.gguf")
            if not shard.is_file() or shard.stat().st_size == 0:
                raise SetupError(f"missing or empty shard: {shard.name}")
    elif path.stat().st_size == 0:
        raise SetupError(f"{path.name} is empty")
    return path


def find_models(directories: Iterable[Path]) -> list[Path]:
    """Complete GGUF models (or first shards) already on disk, largest first."""

    found: dict[Path, int] = {}
    for directory in directories:
        directory = directory.expanduser()
        if not directory.is_dir():
            continue
        for candidate in directory.rglob("*.gguf"):
            try:
                model = complete_model(candidate)
            except (SetupError, OSError):
                continue
            found[model] = model_bytes(model)
    return sorted(found, key=lambda p: -found[p])


def model_bytes(model: Path) -> int:
    match = _SHARD.fullmatch(model.name)
    if not match:
        return model.stat().st_size
    prefix, _, count = match.groups()
    return sum(
        model.with_name(f"{prefix}-{i:05d}-of-{count}.gguf").stat().st_size
        for i in range(1, int(count) + 1)
    )


# -- engine --------------------------------------------------------------------


def patch_files(zhunt_plus: Path) -> list[Path]:
    folder = zhunt_plus / "engine" / "inferssd-patches"
    patches = []
    for prefix in PATCH_PREFIXES:
        matches = sorted(folder.glob(f"{prefix}*.patch"))
        if len(matches) != 1:
            raise SetupError(f"expected one {prefix}*.patch in {folder}, found {len(matches)}")
        patches.append(matches[0])
    return patches


def _fingerprint(patches: Sequence[Path]) -> dict[str, object]:
    return {
        "revision": PINNED_REVISION,
        "patches": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in patches},
    }


def engine_is_built(engine_dir: Path, patches: Sequence[Path]) -> bool:
    marker = engine_dir / MARKER
    try:
        return json.loads(marker.read_text()) == _fingerprint(patches) and (
            engine_dir / "engine-resources.json"
        ).is_file()
    except (OSError, ValueError):
        return False


def prepare_engine(
    *,
    source: Path,
    engine_dir: Path,
    patches: Sequence[Path],
    python: str,
    run: Runner = subprocess.run,
    log: Callable[[str], None] = print,
) -> None:
    """Clone the local InferSSD checkout at the pinned revision, apply the patches,
    build it with Metal and describe the build for the launcher."""

    if not (source / ".git").exists():
        raise SetupError(f"{source} is not a local InferSSD git checkout (nothing is downloaded)")
    if engine_dir.exists():
        raise SetupError(
            f"{engine_dir} already exists but is not a finished build of this version; "
            "move it aside and run setup again"
        )
    engine_dir.parent.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> None:
        run(["git", "-C", str(engine_dir), *args], check=True)

    log(f"Cloning {source} (local folder) into {engine_dir}")
    run(["git", "clone", "--quiet", "--no-checkout", str(source), str(engine_dir)], check=True)
    git("checkout", "--quiet", "--detach", PINNED_REVISION)
    for patch in patches:
        git("apply", "--check", str(patch))
        git("apply", str(patch))
        log(f"Applied {patch.name}")
    build_dir = engine_dir / "build-mac"
    log("Building InferSSD with Metal (about 2 to 12 minutes)")
    run(
        ["bash", str(engine_dir / "scripts" / "build-macos-metal.sh")],
        check=True,
        env={**os.environ, "SSD_LLAMA_BUILD_DIR": str(build_dir)},
    )
    run(
        [python, str(engine_dir / "scripts" / "engine-resources.py"),
         "--build", str(build_dir), "--output", str(engine_dir / "engine-resources.json")],
        check=True,
    )
    (engine_dir / MARKER).write_text(json.dumps(_fingerprint(patches), indent=2) + "\n")


# -- server --------------------------------------------------------------------


@dataclass(frozen=True)
class ServerSettings:
    launcher: str
    python: str
    engine_dir: str
    model: str
    port: int = 8080
    ram_gib: int = 11
    context: int = 1024
    threads: int = 4

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def command(self) -> list[str]:
        return [
            self.python, self.launcher,
            "--engine-dir", self.engine_dir, "--model", self.model,
            "--ram", str(self.ram_gib), "--context", str(self.context),
            "--threads", str(self.threads), "--port", str(self.port),
        ]


def save_settings(settings: ServerSettings, state_dir: Path | None = None) -> None:
    state_dir = state_dir or STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "server.json").write_text(json.dumps(asdict(settings), indent=2) + "\n")


def load_settings(state_dir: Path | None = None) -> ServerSettings:
    state_dir = state_dir or STATE_DIR
    try:
        return ServerSettings(**json.loads((state_dir / "server.json").read_text()))
    except (OSError, ValueError, TypeError) as error:
        raise SetupError("no local model is set up yet; run `zhunt local setup`") from error


def start_server(
    settings: ServerSettings,
    *,
    state_dir: Path | None = None,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> int:
    """Start the server in the background; its output goes to server.log."""

    state_dir = state_dir or STATE_DIR
    if running_pid(state_dir) is not None:
        raise SetupError("the local model server is already running (`zhunt local stop` first)")
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "server.log").open("ab") as log_file:
        process = popen(
            settings.command(),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    (state_dir / "server.pid").write_text(f"{process.pid}\n")
    return process.pid


def running_pid(state_dir: Path | None = None) -> int | None:
    state_dir = state_dir or STATE_DIR
    try:
        pid = int((state_dir / "server.pid").read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    return pid


def stop_server(state_dir: Path | None = None) -> bool:
    state_dir = state_dir or STATE_DIR
    pid = running_pid(state_dir)
    (state_dir / "server.pid").unlink(missing_ok=True)
    # start_new_session made our server a group leader; a pid reused by some
    # other process after a reboot almost never is, so it is left alone.
    if pid is None or os.getpgid(pid) != pid:
        return False
    os.killpg(pid, signal.SIGTERM)
    return True
