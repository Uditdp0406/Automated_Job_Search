"""`jobagent schedule` - cron / launchd / Task Scheduler, with the system commands faked."""
import plistlib
import subprocess
import xml.dom.minidom
from pathlib import Path

import pytest

from jobagent import schedule as sch


def job(tmp_path, every=10, project_name="My Projects"):
    proj = tmp_path / project_name / "Automated_Job_Search"
    (proj / "data").mkdir(parents=True)
    return sch.Job(project=proj, config=proj / "config.yaml", python=proj / ".venv" / "bin" / "python",
                   log_file=proj / "data" / "watch.log", every=every)


class FakeCrontab:
    def __init__(self, existing=None):
        self.text = existing  # None = user has no crontab yet

    def __call__(self, cmd, capture_output=True, text=True, input=None, **kw):
        if cmd == ["crontab", "-l"]:
            if self.text is None:
                return subprocess.CompletedProcess(cmd, 1, "", "no crontab for user\n")
            return subprocess.CompletedProcess(cmd, 0, self.text, "")
        if cmd == ["crontab", "-"]:
            self.text = input
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise FileNotFoundError(cmd[0])


def test_cron_install_is_idempotent_and_keeps_other_jobs(tmp_path):
    cron = FakeCrontab("0 3 * * * backup.sh\n")
    j = job(tmp_path)
    sch.cron_install(j, cron)
    sch.cron_install(j, cron)                      # installing twice must not duplicate
    lines = cron.text.splitlines()
    assert lines[0] == "0 3 * * * backup.sh" and len(lines) == 2
    line = lines[1]
    assert line.startswith("*/10 * * * * cd ") and line.endswith(sch.CRON_TAG)
    assert "'" + str(j.project) + "'" in line      # path with a space is quoted
    assert "_run.py" in line and "watch --once" in line and "--log-file" in line
    assert sch.cron_status(cron) == line
    assert sch.cron_remove(cron) is True
    assert cron.text == "0 3 * * * backup.sh\n"    # only our line removed
    assert sch.cron_remove(cron) is False


def test_cron_first_crontab_hours_and_percent(tmp_path):
    cron = FakeCrontab(None)
    j = job(tmp_path, every=120, project_name="100%-jobs")
    line = sch.cron_install(j, cron)
    assert line.startswith("0 */2 * * * ") and "100\\%-jobs" in line   # % escaped for cron
    with pytest.raises(sch.ScheduleError):
        sch.cron_line(job(tmp_path / "x", every=90))


def test_launchd(tmp_path):
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    j = job(tmp_path, every=15)
    plist = tmp_path / "LaunchAgents" / "com.jobagent.watch.plist"
    sch.launchd_install(j, run, plist_path=plist)
    data = plistlib.loads(plist.read_bytes())
    assert data["StartInterval"] == 900 and data["RunAtLoad"] is True
    assert data["ProgramArguments"][0] == str(j.python) and data["ProgramArguments"][-2:] == ["watch", "--once"]
    assert data["WorkingDirectory"] == str(j.project)
    assert calls[-1][:2] == ["launchctl", "bootstrap"]
    assert "every 15 min" in sch.launchd_status(run, plist_path=plist)
    assert sch.launchd_remove(run, plist_path=plist) is True and not plist.exists()
    assert calls[-1][:2] == ["launchctl", "bootout"]


def test_windows_task_xml_and_schtasks(tmp_path):
    j = job(tmp_path, every=10)
    j.python = tmp_path / "venv" / "Scripts" / "python.exe"
    j.python.parent.mkdir(parents=True)
    (j.python.parent / "pythonw.exe").write_text("")
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "SUCCESS", "")
    sch.task_install(j, run)
    xml_file = j.log_file.parent / "jobagent-task.xml"
    raw = xml_file.read_bytes()
    assert raw[:2] in (b"\xff\xfe", b"\xfe\xff")                  # UTF-16 with BOM, as schtasks expects
    doc = xml.dom.minidom.parseString(xml_file.read_text(encoding="utf-16").encode("utf-16"))
    val = lambda tag: doc.getElementsByTagName(tag)[0].firstChild.data  # noqa: E731
    assert val("Interval") == "PT10M"
    assert val("DisallowStartIfOnBatteries") == "false" and val("StopIfGoingOnBatteries") == "false"
    assert val("StartWhenAvailable") == "true" and val("MultipleInstancesPolicy") == "IgnoreNew"
    assert val("Command").endswith("pythonw.exe")                  # no console window popping up
    assert "watch --once" in val("Arguments") and val("WorkingDirectory") == str(j.project)
    assert calls[0][:4] == ["schtasks", "/Create", "/TN", "jobagent-watch"]
    sch.task_remove(run)
    assert calls[-1][:2] == ["schtasks", "/Delete"]


def test_install_validates_interval(tmp_path):
    with pytest.raises(sch.ScheduleError):
        sch.install(job(tmp_path, every=0), "cron", FakeCrontab())


def test_missing_scheduler_gives_clear_error(tmp_path):
    def run(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    with pytest.raises(sch.ScheduleError, match="crontab"):
        sch.cron_install(job(tmp_path), run)


def test_default_method():
    assert sch.default_method("Windows") == "task-scheduler"
    assert sch.default_method("Darwin") == "launchd"
    assert sch.default_method("Linux") == "cron"


def test_runner_works_from_any_folder(tmp_path):
    import sys
    runner = Path(sch.__file__).with_name("_run.py")
    r = subprocess.run([sys.executable, str(runner), "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0 and "schedule" in r.stdout


def test_scheduled_run_skips_quietly_when_busy(tmp_path, monkeypatch):
    """A scheduled `watch --once` while another watcher holds the lock exits 0 and logs why."""
    from jobagent.cli import main
    from jobagent.laptop import ProcessLock
    from test_offline import profile
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    (tmp_path / "config.yaml").write_text("search:\n  use_web_discovery: false\n  use_remotive: false\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "profile.json").write_text(profile().model_dump_json())
    (tmp_path / "data" / "resume.txt").write_text("r")
    lock = ProcessLock(tmp_path / "data" / "watch.lock")
    lock.acquire()
    import logging
    logging.getLogger().handlers.clear()
    assert main(["--log-file", "data/watch.log", "watch", "--once"]) == 0
    for h in logging.getLogger().handlers:
        h.flush()
    assert "already running" in (tmp_path / "data" / "watch.log").read_text()
    lock.release()
    logging.getLogger().handlers.clear()
