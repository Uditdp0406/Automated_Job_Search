"""Helpers for running the agent on a personal laptop (Windows / macOS / Linux).

* ProcessLock  - OS-level file lock: released automatically if the laptop crashes or Python is killed.
* KeepAwake    - stops *idle* sleep while watching (closing the lid still sleeps; the watcher catches up).
* wait()       - sleeps in short steps so a check runs promptly after the laptop wakes up.
* load_dotenv  - reads API keys from a .env file so you don't set them in every new terminal.
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------- single instance --
class AlreadyRunning(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise AlreadyRunning("the job watcher is already running in another window "
                                 "(close it first, or press Ctrl+C there)") from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


# -------------------------------------------------------------------------------- keep awake --
class KeepAwake:
    """Prevent idle sleep while the watcher runs. Best-effort; never raises."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._proc = None
        self._win = False

    def __enter__(self):
        if not self.enabled:
            return self
        try:
            system = platform.system()
            if system == "Windows":
                import ctypes
                ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                self._win = True
            elif system == "Darwin" and shutil.which("caffeinate"):
                self._proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            elif system == "Linux" and shutil.which("systemd-inhibit"):
                self._proc = subprocess.Popen(
                    ["systemd-inhibit", "--what=idle:sleep", "--who=jobagent", "--why=Watching for new jobs",
                     "--mode=block", "sleep", "infinity"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.debug("keep-awake on")
        except Exception as e:  # noqa: BLE001
            log.debug("keep-awake unavailable: %s", e)
        return self

    def __exit__(self, *exc):
        try:
            if self._win:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            if self._proc:
                self._proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        return False


# ------------------------------------------------------------------------------------- wait --
def wait(seconds: float, step: float = 15.0, clock=time.time, sleep=time.sleep) -> bool:
    """Wait `seconds` of real (wall-clock) time. Returns True if the laptop slept meanwhile.

    time.sleep() alone doesn't count time spent suspended, so after waking it would keep waiting;
    stepping and checking the wall clock lets the next check run within `step` seconds of wake-up."""
    deadline = clock() + seconds
    woke = False
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return woke
        before = clock()
        sleep(min(step, remaining))
        if clock() - before > step + 60:  # far more real time passed than we slept -> suspended
            woke = True
            log.info("laptop woke up after ~%.0f min asleep - checking now", (clock() - before) / 60)
            return woke


# ------------------------------------------------------------------------------- environment --
def load_dotenv(path: str | Path = ".env") -> list[str]:
    """Minimal .env reader (KEY=value lines). Existing environment variables win."""
    p = Path(path)
    if not p.is_file():
        return []
    loaded = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
            loaded.append(key)
    return loaded


def fix_console_encoding() -> None:
    """Windows terminals / redirected output may not be UTF-8; never crash on an emoji."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
