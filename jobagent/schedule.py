"""`jobagent schedule install|remove|status` - run `watch --once` automatically on your laptop.

  Windows : Task Scheduler task "jobagent-watch" (runs on battery, catches up after sleep, no console window)
  Linux   : a line in your user crontab, tagged "# jobagent-watch"
  macOS   : a launchd agent ~/Library/LaunchAgents/com.jobagent.watch.plist (runs missed checks after
            wake; macOS's recommended replacement for cron).  `--method cron` uses crontab instead.

Every run does one check (see watcher.py). Runs never overlap (the watcher's lock); output goes to
data/watch.log (rotated). Keys come from the project's .env file, since scheduled jobs don't see the
variables you set in a terminal.
"""
from __future__ import annotations

import os
import platform
import plistlib
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "jobagent-watch"
CRON_TAG = "# jobagent-watch"
LAUNCHD_LABEL = "com.jobagent.watch"


class ScheduleError(RuntimeError):
    pass


@dataclass
class Job:
    project: Path       # working directory (where config.yaml and .env live)
    config: Path
    python: Path
    log_file: Path
    every: int          # minutes

    def args(self) -> list[str]:
        runner = Path(__file__).with_name("_run.py")
        return [str(runner), "-c", str(self.config), "--log-file", str(self.log_file), "watch", "--once"]


def default_method(system: str | None = None) -> str:
    system = system or platform.system()
    return {"Windows": "task-scheduler", "Darwin": "launchd"}.get(system, "cron")


def _run(cmd, run=subprocess.run, **kw):
    try:
        return run(cmd, capture_output=True, text=True, **kw)
    except FileNotFoundError as e:
        raise ScheduleError(f"`{cmd[0]}` is not available on this system") from e


# ----------------------------------------------------------------------------------------- cron --
def cron_line(job: Job, env: dict | None = None) -> str:
    env = os.environ if env is None else env
    q = lambda p: shlex.quote(str(p)).replace("%", "\\%")  # noqa: E731 - '%' means newline to cron
    if job.every >= 60:
        when = f"0 */{job.every // 60} * * *" if job.every % 60 == 0 and job.every < 1440 else None
        if not when:
            raise ScheduleError("for cron, use an interval of 1-59 minutes or whole hours")
    else:
        when = f"*/{job.every} * * * *"
    # cron jobs have no desktop session: pass what notify-send needs to show pop-ups.
    uid = os.getuid() if hasattr(os, "getuid") else 0
    extras = []
    display = env.get("DISPLAY") or (":0" if platform.system() == "Linux" else "")
    if display:
        extras.append(f"DISPLAY={q(display)}")
    bus = env.get("DBUS_SESSION_BUS_ADDRESS") or (f"unix:path=/run/user/{uid}/bus"
                                                 if Path(f"/run/user/{uid}/bus").exists() else "")
    if bus:
        extras.append(f"DBUS_SESSION_BUS_ADDRESS={q(bus)}")
    cmd = " ".join([*extras, q(job.python), *map(q, job.args())])
    return f"{when} cd {q(job.project)} && {cmd} {CRON_TAG}"


def _crontab_read(run) -> list[str]:
    r = _run(["crontab", "-l"], run)
    if r.returncode != 0:
        if "no crontab" in (r.stderr or "").lower():
            return []
        raise ScheduleError(f"could not read your crontab: {r.stderr.strip()}")
    return r.stdout.splitlines()


def _crontab_write(lines: list[str], run) -> None:
    text = "\n".join(lines).strip("\n") + "\n" if lines else ""
    r = _run(["crontab", "-"], run, input=text)
    if r.returncode != 0:
        raise ScheduleError(f"could not update your crontab: {r.stderr.strip()}")


def cron_install(job: Job, run=subprocess.run) -> str:
    lines = [ln for ln in _crontab_read(run) if CRON_TAG not in ln]
    line = cron_line(job)
    _crontab_write(lines + [line], run)
    return line


def cron_remove(run=subprocess.run) -> bool:
    lines = _crontab_read(run)
    keep = [ln for ln in lines if CRON_TAG not in ln]
    if len(keep) != len(lines):
        _crontab_write(keep, run)
        return True
    return False


def cron_status(run=subprocess.run) -> str | None:
    return next((ln for ln in _crontab_read(run) if CRON_TAG in ln), None)


# -------------------------------------------------------------------------------------- launchd --
def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def launchd_plist(job: Job) -> dict:
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(job.python), *job.args()],
        "WorkingDirectory": str(job.project),
        "StartInterval": job.every * 60,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(job.log_file.with_suffix(".launchd.log")),
        "StandardErrorPath": str(job.log_file.with_suffix(".launchd.log")),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    }


def launchd_install(job: Job, run=subprocess.run, plist_path: Path | None = None) -> Path:
    path = plist_path or launchd_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    launchd_remove(run, path, delete=False)  # reload cleanly if already installed
    with open(path, "wb") as f:
        plistlib.dump(launchd_plist(job), f)
    domain = f"gui/{os.getuid()}"
    r = _run(["launchctl", "bootstrap", domain, str(path)], run)
    if r.returncode != 0:  # older macOS
        r = _run(["launchctl", "load", "-w", str(path)], run)
        if r.returncode != 0:
            raise ScheduleError(f"launchctl could not load the job: {r.stderr.strip()}")
    return path


def launchd_remove(run=subprocess.run, plist_path: Path | None = None, delete: bool = True) -> bool:
    path = plist_path or launchd_plist_path()
    if not path.exists():
        return False
    r = _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], run)
    if r.returncode != 0:
        _run(["launchctl", "unload", "-w", str(path)], run)
    if delete:
        path.unlink()
    return True


def launchd_status(run=subprocess.run, plist_path: Path | None = None) -> str | None:
    path = plist_path or launchd_plist_path()
    if not path.exists():
        return None
    with open(path, "rb") as f:
        every = plistlib.load(f).get("StartInterval", 0) // 60
    loaded = _run(["launchctl", "list", LAUNCHD_LABEL], run).returncode == 0
    return f"launchd agent {path} (every {every} min, {'loaded' if loaded else 'NOT loaded'})"


# ------------------------------------------------------------------------------ Task Scheduler --
def task_xml(job: Job, start: datetime | None = None) -> str:
    start = (start or datetime.now() + timedelta(minutes=1)).replace(microsecond=0)
    py = job.python
    pythonw = py.with_name("pythonw.exe")  # same interpreter without a console window
    command = pythonw if pythonw.exists() else py
    args = subprocess.list2cmdline(job.args())
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>jobagent: check watched companies for new jobs, alert and apply</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT{job.every}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start.isoformat()}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(command))}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(str(job.project))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def task_install(job: Job, run=subprocess.run) -> Path:
    xml_path = job.log_file.parent / "jobagent-task.xml"
    xml_path.write_text(task_xml(job), encoding="utf-16")  # schtasks wants UTF-16 with BOM
    r = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"], run)
    if r.returncode != 0:
        raise ScheduleError(f"Task Scheduler refused the task: {(r.stderr or r.stdout).strip()}")
    return xml_path


def task_remove(run=subprocess.run) -> bool:
    return _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], run).returncode == 0


def task_status(run=subprocess.run) -> str | None:
    r = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"], run)
    if r.returncode != 0:
        return None
    keep = ("Status:", "Next Run Time:", "Last Run Time:", "Last Result:", "Repeat: Every:")
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith(keep)]
    return "Task Scheduler task 'jobagent-watch'\n    " + "\n    ".join(lines)


# ---------------------------------------------------------------------------------- front door --
def install(job: Job, method: str, run=subprocess.run) -> str:
    if not 1 <= job.every <= 1440:
        raise ScheduleError("--every must be between 1 and 1440 minutes")
    for other in {"cron", "launchd", "task-scheduler"} - {method}:
        try:  # switching method: don't leave a second scheduler running the same job
            if other != "task-scheduler" or platform.system() == "Windows":
                remove(other, run)
        except ScheduleError:
            pass
    if method == "cron":
        return f"cron: {cron_install(job, run)}"
    if method == "launchd":
        return f"launchd agent: {launchd_install(job, run)}"
    if method == "task-scheduler":
        task_install(job, run)
        return f"Task Scheduler task '{TASK_NAME}' (every {job.every} min, also on battery)"
    raise ScheduleError(f"unknown method {method!r}")


def remove(method: str, run=subprocess.run) -> bool:
    return {"cron": cron_remove, "launchd": launchd_remove, "task-scheduler": task_remove}[method](run)


def status(method: str, run=subprocess.run) -> str | None:
    return {"cron": cron_status, "launchd": launchd_status, "task-scheduler": task_status}[method](run)


def make_job(config_path: str, data_dir: str, every: int) -> Job:
    project = Path.cwd().resolve()
    data = Path(data_dir)
    data = (data if data.is_absolute() else project / data).resolve()
    return Job(project=project, config=Path(config_path).resolve(), python=Path(sys.executable),
               log_file=data / "watch.log", every=every)
