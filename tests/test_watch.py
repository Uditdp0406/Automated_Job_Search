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
    w = env["watcher"]
    sleeps = []
    w.run(max_cycles=2, sleep=sleeps.append)
    assert len(sleeps) == 1 and 1.8 * 60 <= sleeps[0] <= 11.5 * 60
    assert not (Path(w.s.data_dir) / "watch.lock").exists()
    assert env["n"].titles()[0] == "👀 Job watch started"


def test_lock_blocks_second_watcher(tmp_path):
    from jobagent.watcher import AlreadyRunning, WatchLock
    lock = tmp_path / "watch.lock"
    lock.write_text(str(os.getppid()))  # a live process that isn't us
    with pytest.raises(AlreadyRunning):
        WatchLock(lock, stale_after_s=3600).acquire()
    lock.write_text("999999999")         # dead pid -> stale lock is taken over
    WatchLock(lock, stale_after_s=3600).acquire()
    assert lock.read_text() == str(os.getpid())


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
