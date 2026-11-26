"""Watch mode + alerts - offline (sources, Claude, browser and alert channels are faked)."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobagent import sources
from jobagent.applier import ApplyResult
from jobagent.config import Settings
from jobagent.models import FitAssessment, JobPosting
from test_offline import profile

NOW = datetime.now(timezone.utc)


def posting(board, jid, title, days_old=30, source="greenhouse", desc=""):
    return JobPosting(source=source, source_id=f"{board}/{jid}", company=board.title(), title=title,
                      location="Bengaluru, India", url=f"https://x/{board}/{jid}", apply_url=f"https://x/{board}/{jid}",
                      description=desc, posted_at=(NOW - timedelta(days=days_old)).isoformat())


class FakeLLM:
    def __init__(self):
        self.fail_titles = set()

    def parse(self, *, schema, user, **_):
        title = user.split("Title:")[1].split("\n")[0].strip()
        if title in self.fail_titles:
            raise RuntimeError("API down")
        score = 90 if "Backend" in title else 50
        return FitAssessment(score=score, recommendation="apply" if score > 70 else "skip", matched_requirements=["Python"],
                             missing_requirements=[], dealbreakers=[], reasoning="", keywords_to_emphasize=[])


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, title, message, url="", urgent=False):
        self.sent.append(title)
        return {"fake": True}

    def titles(self):
        out, self.sent = self.sent, []
        return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    from jobagent.pipeline import Agent
    from jobagent.watcher import Watcher
    s = Settings.model_validate({
        "data_dir": str(tmp_path / "d"), "output_dir": str(tmp_path / "o"),
        "search": {"greenhouse_boards": ["acme"], "lever_companies": ["beta"], "use_remotive": False,
                   "use_web_discovery": False, "locations": ["Bengaluru"]},
        "apply": {"min_score": 70},
    })
    Path(s.data_dir).mkdir(); Path(s.output_dir).mkdir()
    agent = Agent(s)
    agent.llm = FakeLLM()
    s.profile_path.write_text(profile().model_dump_json()); s.resume_text_path.write_text("resume")

    boards = {("greenhouse", "acme"): [posting("acme", 1, "Backend Engineer")],
              ("lever", "beta"): [posting("beta", 1, "Python Developer", source="lever")]}
    monkeypatch.setattr(sources, "poll_greenhouse", lambda b, with_content=True: list(boards[("greenhouse", b)]))
    monkeypatch.setattr(sources, "poll_lever", lambda c: list(boards[("lever", c)]))
    details = []
    monkeypatch.setattr(sources, "greenhouse_details",
                        lambda j: details.append(j.key) or j.model_copy(update={"description": "Python, Django"}))

    def fake_tailor(key):
        d = tmp_path / "o" / key.replace("/", "_").replace(":", "_")
        d.mkdir(parents=True, exist_ok=True)
        (d / "r.pdf").write_bytes(b"%PDF"); (d / "c.txt").write_text("hi")
        agent.tracker.update(key, status="tailored", resume_path=str(d / "r.pdf"), cover_letter_path=str(d / "c.txt"))
        return d / "r.pdf", d / "c.txt"
    monkeypatch.setattr(agent, "tailor", fake_tailor)
    applied = []
    monkeypatch.setattr("jobagent.applier.apply_to_job",
                        lambda llm, job, *a, **k: applied.append((job.key, k)) or ApplyResult("applied", "ok"))
    n = FakeNotifier()
    return dict(agent=agent, watcher=Watcher(agent, notifier=n), boards=boards, n=n, applied=applied, details=details)


def test_baseline_then_alert_and_apply_on_new_job(env):
    w, t, boards, n = env["watcher"], env["agent"].tracker, env["boards"], env["n"]
    st = w.cycle()                                    # first check: existing jobs are only recorded
    assert st.boards_polled == 2 and st.new_postings == 0 and not env["applied"]
    assert t.all() == [] and not n.titles()

    boards[("greenhouse", "acme")].append(posting("acme", 2, "Senior Backend Engineer", days_old=0))
    boards[("greenhouse", "acme")].append(posting("acme", 3, "Sales Manager", days_old=0))
    st = w.cycle()                                    # a job opens -> alert, tailor, apply
    assert st.new_postings == 2 and st.relevant == 1
    assert env["details"] == ["greenhouse:acme/2"]    # full description fetched only for the relevant new job
    assert [k for k, _ in env["applied"]] == ["greenhouse:acme/2"]
    assert env["applied"][0][1]["interactive"] is False and env["applied"][0][1]["auto_submit"] is True
    titles = n.titles()
    assert titles[0].startswith("🎯 New job match 90/100") and titles[1] == "✅ Applied"
    row = t.get("greenhouse:acme/2")
    assert row["status"] == "applied" and row["applied_at"]

    st = w.cycle()                                    # nothing new -> silence
    assert st.new_postings == 0 and not n.titles() and len(env["applied"]) == 1


def test_fresh_jobs_count_as_new_at_baseline(env):
    env["boards"][("lever", "beta")] = [posting("beta", 9, "Backend Engineer", days_old=0, source="lever")]
    env["watcher"].cycle()
    assert [k for k, _ in env["applied"]] == ["lever:beta/9"]


def test_daily_cap(env):
    w, boards = env["watcher"], env["boards"]
    w.cfg.max_applications_per_day = 1
    w.cycle()
    boards[("greenhouse", "acme")] += [posting("acme", 5, "Backend Engineer II", 0), posting("acme", 6, "Backend Lead", 0)]
    w.cycle()
    assert len(env["applied"]) == 1
    assert "⏸ Daily application cap reached" in env["n"].titles()
    statuses = sorted(r["status"] for r in env["agent"].tracker.all())
    assert statuses == ["applied", "scored"]          # the second one is kept for later


def test_review_mode_does_not_submit(env):
    w, boards = env["watcher"], env["boards"]
    w.cfg.auto_submit = False
    w.cycle()
    boards[("greenhouse", "acme")].append(posting("acme", 7, "Backend Engineer", 0))
    w.cycle()
    assert env["applied"] == []
    assert "📝 Application ready for your review" in env["n"].titles()
    assert env["agent"].tracker.get("greenhouse:acme/7")["status"] == "tailored"


def test_scoring_failure_is_retried(env):
    w, boards, llm = env["watcher"], env["boards"], env["agent"].llm
    w.cycle()
    boards[("greenhouse", "acme")].append(posting("acme", 8, "Backend Engineer", 0))
    llm.fail_titles.add("Backend Engineer")
    w.cycle()
    assert env["agent"].tracker.get("greenhouse:acme/8")["status"] == "found" and not env["applied"]
    llm.fail_titles.clear()
    w.cycle()
    assert [k for k, _ in env["applied"]] == ["greenhouse:acme/8"]


def test_missing_board_is_disabled(env, monkeypatch):
    def gh(b, with_content=True):
        raise sources.BoardNotFound(b)
    monkeypatch.setattr(sources, "poll_greenhouse", gh)
    st = env["watcher"].cycle()
    assert st.board_errors == 1
    assert [r["slug"] for r in env["agent"].tracker.boards()] == ["beta"]


def test_watch_loop_runs_and_releases_lock(env):
    from jobagent.laptop import ProcessLock
    w = env["watcher"]
    sleeps = []
    w.run(max_cycles=2, sleep=sleeps.append)
    assert len(sleeps) == 1 and 1.8 * 60 <= sleeps[0] <= 11.5 * 60
    lock = ProcessLock(Path(w.s.data_dir) / "watch.lock")
    lock.acquire(); lock.release()  # released -> can be taken again
    assert env["n"].titles()[0] == "👀 Job watch started"


def test_lock_blocks_second_watcher(tmp_path):
    from jobagent.laptop import AlreadyRunning, ProcessLock
    first = ProcessLock(tmp_path / "watch.lock")
    first.acquire()
    with pytest.raises(AlreadyRunning):
        ProcessLock(tmp_path / "watch.lock").acquire()
    first.release()
    ProcessLock(tmp_path / "watch.lock").acquire()  # a stale lock file alone never blocks


def test_lock_is_freed_when_process_dies(tmp_path):
    import subprocess, sys
    code = (f"import sys; sys.path.insert(0, {str(Path(__file__).parents[1])!r});"
            f"from jobagent.laptop import ProcessLock; ProcessLock(__import__('pathlib').Path({str(tmp_path / 'w.lock')!r})).acquire();"
            "import os; os._exit(9)")  # simulates a crash: no cleanup runs
    subprocess.run([sys.executable, "-c", code], check=False)
    from jobagent.laptop import ProcessLock
    ProcessLock(tmp_path / "w.lock").acquire()


def test_offline_cycle_is_quiet_and_retries_soon(env, monkeypatch):
    import httpx
    w = env["watcher"]
    def down(*a, **k):
        raise httpx.ConnectError("no internet")
    monkeypatch.setattr(sources, "poll_greenhouse", down)
    monkeypatch.setattr(sources, "poll_lever", down)
    sleeps = []
    w.run(max_cycles=2, sleep=sleeps.append)
    assert sleeps == [60.0]                                  # quick retry, not the full interval
    titles = env["n"].titles()
    assert not any("error" in t.lower() for t in titles)     # no error alerts for being offline
    assert all(b["last_error"] is None and not b["disabled"] for b in env["agent"].tracker.boards())
    assert env["agent"].tracker.meta_get("last_check") is None


def test_catch_up_after_laptop_was_off(env):
    w, t = env["watcher"], env["agent"].tracker
    w.cycle()
    t.meta_set("last_check", (NOW - timedelta(hours=14)).isoformat(timespec="seconds"))
    env["boards"][("greenhouse", "acme")].append(posting("acme", 11, "Backend Engineer", days_old=0))
    w.run(max_cycles=1, sleep=lambda s: None)
    titles = env["n"].sent
    started = titles[0]
    assert started == "👀 Job watch started"
    assert [k for k, _ in env["applied"]] == ["greenhouse:acme/11"]  # posted while the laptop was off


def test_wait_detects_wake_from_sleep():
    from jobagent.laptop import wait
    t = [1000.0]
    calls = []
    def clock():
        return t[0]
    def sleep(sec):
        calls.append(sec)
        t[0] += sec + (3600 if len(calls) == 2 else 0)  # laptop suspended for an hour during the 2nd step
    assert wait(600, step=15, clock=clock, sleep=sleep) is True
    assert len(calls) == 2                                   # returned right after waking, not 600s later
    t[0], calls[:] = 0.0, []
    def normal(sec):
        calls.append(sec); t[0] += sec
    assert wait(100, step=15, clock=clock, sleep=normal) is False and sum(calls) == pytest.approx(100)


def test_dotenv(tmp_path, monkeypatch):
    from jobagent.laptop import load_dotenv
    (tmp_path / ".env").write_text('# keys\nANTHROPIC_API_KEY="sk-test"\nexport NTFY_TOKEN=abc\nALREADY=new\n')
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False); monkeypatch.delenv("NTFY_TOKEN", raising=False)
    monkeypatch.setenv("ALREADY", "old")
    assert sorted(load_dotenv(tmp_path / ".env")) == ["ANTHROPIC_API_KEY", "NTFY_TOKEN"]
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test" and os.environ["ALREADY"] == "old"


def test_windows_toast(monkeypatch):
    import xml.dom.minidom
    from jobagent import notify
    from jobagent.notify import Notifier, NotifyConfig
    ran = []
    monkeypatch.setattr(notify.platform, "system", lambda: "Windows")
    monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **kw: ran.append((cmd, kw)))
    n = Notifier(NotifyConfig(console=False, desktop=True))
    assert n.send('🎯 New match 90/100 · R&D "Co"', "Matched: <Python>", url="https://x/1?a=1&b=2", urgent=True) == {"desktop": True}
    cmd, kw = ran[0]
    assert cmd[0] == "powershell" and "ToastNotificationManager" in cmd[-1]
    doc = xml.dom.minidom.parseString(kw["env"]["JOBAGENT_TOAST_XML"])  # well-formed despite & < " and emoji
    toast = doc.documentElement
    assert toast.getAttribute("launch") == "https://x/1?a=1&b=2" and toast.getAttribute("scenario") == "reminder"


def test_pending_walkthrough(env, monkeypatch, capsys):
    from jobagent.cli import _pending
    t = env["agent"].tracker
    t.add(posting("acme", 21, "Backend Engineer")); t.update("greenhouse:acme/21", status="filled", score=88)
    t.add(posting("remo", 22, "Backend Engineer", source="remotive"))
    t.update("remotive:remo/22", status="manual", score=80, resume_path="r.pdf")
    applied_keys, opened = [], []
    monkeypatch.setattr(env["agent"], "apply", lambda keys, auto_submit: applied_keys.append((keys, auto_submit)))
    answers = iter(["", "y"])
    _pending(env["agent"], ask=lambda _: next(answers), open_url=opened.append)
    assert applied_keys == [(["greenhouse:acme/21"], False)]   # reopened in review mode, never auto-submitted
    assert env["agent"].s.apply.headless is False
    assert opened == ["https://x/remo/22"] and t.get("remotive:remo/22")["status"] == "applied"


# ------------------------------------------------------------------------------ alert channels --
class FakeHTTP:
    def __init__(self, fail_hosts=()):
        self.calls, self.fail_hosts = [], fail_hosts

    def post(self, url, **kw):
        self.calls.append((url, kw))
        import httpx
        code = 500 if any(h in url for h in self.fail_hosts) else 200
        return httpx.Response(code, request=httpx.Request("POST", url))


def test_notifier_channels_and_isolation(monkeypatch, capsys):
    from jobagent.notify import Notifier, NotifyConfig
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("JOBAGENT_WEBHOOK_URL", "https://discord.com/api/webhooks/x")
    n = Notifier(NotifyConfig(console=True, desktop=False, ntfy_topic="my-secret-topic", telegram=True, webhook=True))
    n.http = FakeHTTP(fail_hosts=("telegram",))
    res = n.send("🎯 New job match 90/100 · Acme — Backend", "Matched: Python", url="https://x/1", urgent=True)
    assert res == {"console": True, "ntfy": True, "telegram": False, "webhook": True}
    ntfy = next(kw for u, kw in n.http.calls if "ntfy" in u)["json"]
    assert ntfy["topic"] == "my-secret-topic" and ntfy["priority"] == 5 and ntfy["click"] == "https://x/1"
    assert ntfy["title"].startswith("🎯")
    hook = next(kw for u, kw in n.http.calls if "discord" in u)["json"]
    assert "content" in hook and "Acme" in hook["content"]
    assert "Backend" in capsys.readouterr().out
