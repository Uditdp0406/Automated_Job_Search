"""`jobagent watch`: get alerted the moment a matching job opens, and apply to it right away.

Job boards don't push notifications, so the watcher polls the public Greenhouse / Lever APIs of every
company on the watch list every few minutes (cheap, no Claude calls). Only postings it has never seen
before go through prefilter -> Claude scoring -> alert -> tailor -> apply.

  * First poll of a board = baseline: jobs already open are marked seen (not applied to), except ones
    posted within `watch.fresh_hours`, unless `include_existing_on_first_run` is on.
  * Watch list = config boards + boards found by periodic Claude web discovery.
  * Safety: daily application cap, the applier's required-field gate, a lock against double runs.
"""
from __future__ import annotations

import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import sources
from .matcher import Matcher, prefilter
from .models import JobPosting
from .notify import Notifier

log = logging.getLogger(__name__)
MIN_INTERVAL_MIN = 2.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class AlreadyRunning(RuntimeError):
    pass


class WatchLock:
    """Lock file so cron / Task Scheduler / a second terminal can't run two watchers at once."""

    def __init__(self, path: Path, stale_after_s: float):
        self.path, self.stale_after_s = path, stale_after_s

    def _alive(self, pid: int) -> bool:
        if os.name != "posix":
            return True  # rely on staleness by age on Windows
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def acquire(self) -> None:
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip() or 0)
            except ValueError:
                pid = 0
            age = time.time() - self.path.stat().st_mtime
            if pid and pid != os.getpid() and self._alive(pid) and age < self.stale_after_s:
                raise AlreadyRunning(f"another watcher (pid {pid}) is running; lock: {self.path}")
        self.path.write_text(str(os.getpid()))

    def touch(self) -> None:
        self.path.write_text(str(os.getpid()))

    def release(self) -> None:
        try:
            if self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()
        except FileNotFoundError:
            pass


@dataclass
class CycleStats:
    boards_polled: int = 0
    board_errors: int = 0
    new_postings: int = 0
    relevant: int = 0
    matches: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    needs_you: list[str] = field(default_factory=list)


class Watcher:
    def __init__(self, agent, notifier: Notifier | None = None):
        self.agent = agent
        self.s = agent.s
        self.cfg = agent.s.watch
        self.tracker = agent.tracker
        self.notifier = notifier or Notifier(agent.s.notify)
        self.min_score = self.cfg.min_score if self.cfg.min_score is not None else self.s.apply.min_score
        self.alert_min = self.cfg.alert_min_score if self.cfg.alert_min_score is not None else self.min_score

    # ------------------------------------------------------------------ watch list --
    def sync_boards(self) -> None:
        for b in self.s.search.greenhouse_boards:
            self.tracker.add_board("greenhouse", b, "config")
        for c in self.s.search.lever_companies:
            self.tracker.add_board("lever", c, "config")

    def _due(self, meta_key: str, every_hours: float) -> bool:
        if every_hours <= 0:
            return False
        last = self.tracker.meta_get(meta_key)
        return last is None or _now() - datetime.fromisoformat(last) >= timedelta(hours=every_hours)

    def maybe_discover(self, profile) -> int:
        if not (self.s.search.use_web_discovery and self._due("last_discovery", self.cfg.discovery_every_hours)):
            return 0
        titles = list(dict.fromkeys(self.s.search.titles + profile.target_titles))
        added = 0
        try:
            gh, lv = sources.discover_boards(self.agent.llm, titles, self.s.search.locations)
            added = sum(self.tracker.add_board("greenhouse", b, "discovered") for b in gh)
            added += sum(self.tracker.add_board("lever", c, "discovered") for c in lv)
            log.info("discovery: %d new companies added to the watch list", added)
        except Exception as e:  # noqa: BLE001 - discovery is optional
            log.warning("discovery failed: %s", e)
        self.tracker.meta_set("last_discovery", _iso(_now()))
        return added

    # ----------------------------------------------------------------------- polling --
    @staticmethod
    def _poll(source: str, slug: str):
        try:
            if source == "greenhouse":
                return source, slug, sources.poll_greenhouse(slug, with_content=False), None
            return source, slug, sources.poll_lever(slug), None
        except Exception as e:  # noqa: BLE001 - recorded per board
            return source, slug, None, e

    def _is_fresh(self, job: JobPosting) -> bool:
        age = sources.age_days(job.posted_at)
        return age is not None and age * 24 <= self.cfg.fresh_hours

    def _collect_new(self, profile, stats: CycleStats) -> list[JobPosting]:
        boards = self.tracker.boards()
        results = []
        with ThreadPoolExecutor(max_workers=max(1, self.cfg.poll_workers)) as ex:
            results = list(ex.map(lambda b: self._poll(b["source"], b["slug"]), boards))
        baselined = {(b["source"], b["slug"]): b["baselined_at"] for b in boards}

        batches: list[tuple[list[JobPosting], bool, str, str]] = []
        for source, slug, jobs, err in results:
            stats.boards_polled += 1
            if err is not None:
                stats.board_errors += 1
                if isinstance(err, sources.BoardNotFound):
                    log.warning("%s board %r does not exist - removed from watch list", source, slug)
                    self.tracker.board_update(source, slug, disabled=1, last_error="404 not found")
                else:
                    log.warning("%s board %r: %s", source, slug, err)
                    self.tracker.board_update(source, slug, last_error=str(err)[:300])
                continue
            first = baselined[(source, slug)] is None
            batches.append((jobs, first, source, slug))
            self.tracker.board_update(source, slug, last_polled_at=_iso(_now()), last_error=None,
                                      **({"baselined_at": _iso(_now())} if first else {}))

        # Remotive: polled rarely; postings there can only be applied to manually.
        if self.s.search.use_remotive and self._due("last_remotive", self.cfg.remotive_every_hours):
            titles = list(dict.fromkeys(self.s.search.titles + profile.target_titles))
            jobs = []
            for q in list(dict.fromkeys(titles[:1] + (self.s.search.keywords or profile.search_keywords)[:1])):
                jobs += sources.fetch_remotive(q)
            first = self.tracker.meta_get("remotive_baselined") is None
            batches.append((jobs, first, "remotive", "*"))
            self.tracker.meta_set("last_remotive", _iso(_now()))
            self.tracker.meta_set("remotive_baselined", _iso(_now()))

        new: list[JobPosting] = []
        baseline_total, baseline_boards = [0], [0]
        for jobs, first, source, slug in batches:
            fresh_keys = self.tracker.unseen([j.key for j in jobs])
            unseen = [j for j in jobs if j.key in fresh_keys]
            if first and not self.cfg.include_existing_on_first_run:
                keep = [j for j in unseen if self._is_fresh(j)]
                log.debug("baseline %s/%s: %d open postings recorded, %d fresh kept", source, slug,
                          len(unseen), len(keep))
                baseline_total[0] += len(unseen)
                baseline_boards[0] += 1
                unseen = keep
                self.tracker.mark_seen(list(fresh_keys - {j.key for j in keep}))
            new += unseen
        if baseline_boards[0]:
            log.info("first check of %d board(s): %d jobs already open were recorded (not applied to); "
                     "from now on new postings trigger alerts", baseline_boards[0], baseline_total[0])
        stats.new_postings = len(new)
        return new

    def _relevant(self, new: list[JobPosting], profile) -> list[JobPosting]:
        out = []
        for job in new:
            keep, why = prefilter(job, profile, self.s.search)  # title / location (no description yet)
            if keep and job.source == "greenhouse" and not job.description:
                try:
                    job = sources.greenhouse_details(job)
                    keep, why = prefilter(job, profile, self.s.search)  # now with description
                except Exception as e:  # noqa: BLE001 - retry next cycle (not marked seen)
                    log.warning("could not load details for %s: %s", job.key, e)
                    continue
            self.tracker.mark_seen([job.key])
            if keep and self.tracker.add(job):
                self.tracker.update(job.key, notes="watch")
                out.append(job)
            elif not keep:
                log.debug("new posting ignored (%s): %s — %s", why, job.company, job.title)
        return out

    # ---------------------------------------------------------------- per-match work --
    def _applied_last_24h(self) -> int:
        return self.tracker.applied_since(_iso(_now() - timedelta(hours=24)))

    @staticmethod
    def _headline(job: JobPosting, fit) -> str:
        return f"{fit.score}/100 · {job.company} — {job.title}"

    def alert_match(self, job: JobPosting, fit, stats: CycleStats) -> None:
        headline = self._headline(job, fit)
        why = (f"{job.location or 'location n/a'}\nMatched: {', '.join(fit.matched_requirements[:5]) or '-'}\n"
               f"Missing: {', '.join(fit.missing_requirements[:4]) or '-'}")
        qualifies = fit.score >= self.min_score
        self.notifier.send(f"🎯 New job match {headline}", why + ("" if qualifies else "\n(alert only - below apply "
                           f"threshold {self.min_score})"), url=job.url, urgent=qualifies)
        stats.matches.append(headline)

    def handle_match(self, job: JobPosting, fit, profile, stats: CycleStats) -> None:
        """Tailor + apply for a match that was already alerted."""
        from .applier import apply_to_job

        headline = self._headline(job, fit)
        if not (fit.score >= self.min_score and self.cfg.auto_apply):
            return

        if self._applied_last_24h() >= self.cfg.max_applications_per_day:
            self.notifier.send("⏸ Daily application cap reached",
                               f"{headline}\nNot applied (cap {self.cfg.max_applications_per_day}/24h). "
                               f"Run later:  jobagent apply --job \"{job.key}\"", url=job.url)
            return
        try:
            resume_path, cover_path = self.agent.tailor(job.key)
        except Exception as e:  # noqa: BLE001
            self.tracker.update(job.key, status="failed", notes=f"tailoring failed: {e}"[:500])
            self.notifier.send("❌ Tailoring failed", f"{headline}\n{e}", url=job.url)
            return

        if not job.can_auto_apply:
            self.tracker.update(job.key, status="manual", notes=f"apply at {job.apply_url or job.url}")
            self.notifier.send("📝 Apply manually (resume ready)", f"{headline}\nTailored resume: {resume_path}",
                               url=job.apply_url or job.url, urgent=True)
            stats.needs_you.append(headline)
            return
        if not self.cfg.auto_submit:
            self.notifier.send("📝 Application ready for your review",
                               f"{headline}\nResume: {resume_path}\nRun:  jobagent apply --job \"{job.key}\"",
                               url=job.url, urgent=True)
            stats.needs_you.append(headline)
            return

        res = apply_to_job(self.agent.llm, job, profile, resume_path, cover_path, self.s.apply.real_answers(),
                           auto_submit=True, headless=True, interactive=False, screenshot_dir=resume_path.parent)
        self.tracker.update(job.key, status=res.status, notes=res.notes)
        if res.status == "applied":
            stats.applied.append(headline)
            self.notifier.send("✅ Applied", f"{headline}\nResume: {resume_path.name}", url=job.url)
        elif res.status == "filled":
            stats.needs_you.append(headline)
            self.notifier.send("⚠️ Almost applied - needs you", f"{headline}\n{res.notes[-300:]}\n"
                               f"Finish:  jobagent apply --job \"{job.key}\"", url=job.apply_url or job.url,
                               urgent=True)
        else:
            self.notifier.send(f"❌ Application {res.status}", f"{headline}\n{res.notes[:300]}",
                               url=job.apply_url or job.url)

    # -------------------------------------------------------------------- one cycle --
    def cycle(self) -> CycleStats:
        stats = CycleStats()
        profile, _ = self.agent.load_profile()
        self.sync_boards()
        self.maybe_discover(profile)
        new = self._collect_new(profile, stats)
        relevant = self._relevant(new, profile)
        # Retry jobs whose scoring failed in an earlier cycle.
        pending = [self.tracker.posting(r["key"]) for r in self.tracker.by_status("found")
                   if r["notes"] == "watch" and r["key"] not in {j.key for j in relevant}]
        to_score = relevant + pending
        stats.relevant = len(relevant)
        if not to_score:
            return stats

        matcher = Matcher(self.agent.llm, profile, self.s.search)

        def one(job):
            try:
                return job, matcher.assess(job)
            except Exception as e:  # noqa: BLE001 - stays 'found', retried next cycle
                log.warning("scoring failed for %s: %s", job.key, e)
                return job, None

        with ThreadPoolExecutor(max_workers=max(1, self.s.search.scoring_workers)) as ex:
            scored = list(ex.map(one, to_score))
        scored.sort(key=lambda t: t[1].score if t[1] else -1, reverse=True)  # best matches applied first
        matches = []
        for job, fit in scored:
            if fit is None:
                continue
            ok = fit.recommendation != "skip" and not fit.dealbreakers
            status = "scored" if ok and fit.score >= self.min_score else "skipped"
            self.tracker.set_assessment(job.key, fit, status)
            log.info("new posting scored %3d %-5s %s — %s", fit.score, fit.recommendation, job.company, job.title)
            if ok and fit.score >= self.alert_min:
                self.alert_match(job, fit, stats)  # alert every match first - applying takes a while
                matches.append((job, fit))
        for job, fit in matches:
            self.handle_match(job, fit, profile, stats)
        return stats

    # ---------------------------------------------------------------------- loop --
    def run(self, once: bool = False, max_cycles: int | None = None, sleep=time.sleep) -> None:
        interval = max(MIN_INTERVAL_MIN, self.cfg.interval_minutes) * 60
        lock = WatchLock(Path(self.s.data_dir) / "watch.lock", stale_after_s=max(3 * interval, 3600))
        lock.acquire()
        self.sync_boards()
        n_boards = len(self.tracker.boards())
        if not once:
            self.notifier.send("👀 Job watch started",
                               f"Watching {n_boards} companies every {interval / 60:g} min. "
                               f"Auto-apply: {'on' if self.cfg.auto_apply and self.cfg.auto_submit else 'review mode'}"
                               f", min score {self.min_score}, max {self.cfg.max_applications_per_day}/day.")
        if n_boards == 0 and not self.s.search.use_web_discovery:
            log.warning("watch list is empty: add search.greenhouse_boards / lever_companies or enable discovery")
        cycles = 0
        try:
            while True:
                cycles += 1
                lock.touch()
                started = time.monotonic()
                try:
                    st = self.cycle()
                    log.info("check #%d: %d boards (%d errors), %d new postings, %d relevant, %d matches, "
                             "%d applied, %d need you", cycles, st.boards_polled, st.board_errors, st.new_postings,
                             st.relevant, len(st.matches), len(st.applied), len(st.needs_you))
                except SystemExit:
                    raise
                except Exception as e:  # noqa: BLE001 - keep watching; alert at most hourly
                    log.exception("watch cycle failed")
                    if self._due("last_error_alert", 1):
                        self.notifier.send("❌ Job watch error", str(e)[:400], urgent=True)
                        self.tracker.meta_set("last_error_alert", _iso(_now()))
                if once or (max_cycles and cycles >= max_cycles):
                    break
                wait = max(0.0, interval * random.uniform(0.9, 1.1) - (time.monotonic() - started))
                log.info("next check in %.1f min (Ctrl+C to stop)", wait / 60)
                sleep(wait)
        except KeyboardInterrupt:
            log.info("watch stopped")
        finally:
            lock.release()
