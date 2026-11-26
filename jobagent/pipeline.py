"""Orchestrates the end-to-end flow: analyze -> search -> score -> tailor -> apply."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import sources
from .config import Settings
from .llm import LLM
from .matcher import Matcher, prefilter, relevance
from .models import CandidateProfile, JobPosting
from .resume import analyze_resume, extract_text
from .tracker import Tracker

log = logging.getLogger(__name__)


class Agent:
    def __init__(self, settings: Settings):
        self.s = settings
        self.llm = LLM(settings.model)
        self.tracker = Tracker(settings.db_path)

    # ------------------------------------------------------------ analyze --
    def analyze(self, resume_file: str | Path) -> CandidateProfile:
        text = extract_text(resume_file)
        self.s.resume_text_path.write_text(text, encoding="utf-8")
        profile = analyze_resume(self.llm, text)
        self.s.profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
        return profile

    def load_profile(self) -> tuple[CandidateProfile, str]:
        if not self.s.profile_path.exists():
            raise SystemExit("No analyzed resume yet. Run:  jobagent analyze --resume <file>")
        profile = CandidateProfile.model_validate_json(self.s.profile_path.read_text())
        return profile, self.s.resume_text_path.read_text(encoding="utf-8")

    # ------------------------------------------------------------- search --
    def search(self) -> list[JobPosting]:
        profile, _ = self.load_profile()
        cfg = self.s.search
        titles = list(dict.fromkeys(cfg.titles + profile.target_titles))
        gh, lv = set(cfg.greenhouse_boards), set(cfg.lever_companies)
        if cfg.use_web_discovery:
            try:
                dgh, dlv = sources.discover_boards(self.llm, titles, cfg.locations)
                log.info("web discovery found %d greenhouse boards, %d lever companies", len(dgh), len(dlv))
                gh |= dgh
                lv |= dlv
            except Exception as e:  # noqa: BLE001 - discovery is best-effort
                log.warning("web discovery failed: %s", e)

        found: list[JobPosting] = []
        for b in sorted(gh):
            found += sources.fetch_greenhouse(b)
        for c in sorted(lv):
            found += sources.fetch_lever(c)
        if cfg.use_remotive:
            # Remotive asks API users to keep request volume low - two searches per run.
            for q in list(dict.fromkeys(titles[:1] + (cfg.keywords or profile.search_keywords)[:1])):
                found += sources.fetch_remotive(q)

        new, seen = [], set()
        for job in found:
            if job.key in seen or self.tracker.has(job.key):
                continue
            seen.add(job.key)
            keep, why = prefilter(job, profile, cfg)
            if keep:
                new.append(job)
            else:
                log.debug("prefilter drop %s: %s", job.title, why)
        log.info("%d postings fetched, %d new after prefilter", len(found), len(new))
        for job in new:
            self.tracker.add(job)
        return new

    # -------------------------------------------------------------- score --
    def score(self, limit: int | None = None) -> int:
        profile, _ = self.load_profile()
        cfg = self.s.search
        matcher = Matcher(self.llm, profile, cfg)
        jobs = [self.tracker.posting(r["key"]) for r in self.tracker.by_status("found")]
        # Spend the Claude budget on the most promising postings first.
        jobs.sort(key=lambda j: relevance(j, profile, cfg), reverse=True)
        jobs = jobs[: (limit or cfg.max_jobs_to_score)]
        if not jobs:
            log.info("nothing to score - run `search` first")
            return 0

        def one(job: JobPosting):
            try:
                return job, matcher.assess(job)
            except Exception as e:  # noqa: BLE001 - leave it as 'found' to retry next run
                log.warning("scoring failed for %s: %s", job.key, e)
                return job, None

        # First call alone so it writes the prompt cache; the rest run in parallel and read it.
        results = [one(jobs[0])]
        with ThreadPoolExecutor(max_workers=max(1, cfg.scoring_workers)) as ex:
            results += list(ex.map(one, jobs[1:]))

        for job, fit in results:
            if fit is None:
                continue
            ok = fit.recommendation != "skip" and not fit.dealbreakers and fit.score >= self.s.apply.min_score
            self.tracker.set_assessment(job.key, fit, "scored" if ok else "skipped")
            log.info("%3d  %-6s %s — %s", fit.score, fit.recommendation, job.company, job.title)
        return len(jobs)

    # ------------------------------------------------------------- tailor --
    def tailor(self, key: str) -> tuple[Path, Path]:
        from .tailor import build_application_docs

        profile, text = self.load_profile()
        job = self.tracker.posting(key)
        resume_path, cover_path, _ = build_application_docs(
            self.llm, text, profile, job, self.tracker.assessment(key), self.s.output_dir)
        self.tracker.update(key, status="tailored", resume_path=str(resume_path), cover_letter_path=str(cover_path))
        return resume_path, cover_path

    # -------------------------------------------------------------- apply --
    def apply(self, limit: int | None = None, auto_submit: bool | None = None, keys: list[str] | None = None) -> None:
        from .applier import apply_to_job

        profile, _ = self.load_profile()
        cfg = self.s.apply
        auto = cfg.auto_submit if auto_submit is None else auto_submit
        if keys:
            rows = []
            for k in keys:
                row = self.tracker.get(k)
                if row is None:
                    log.warning("unknown job key %r (see `status`)", k)
                elif row["status"] == "applied":
                    log.warning("already applied to %s — skipping", k)
                else:
                    rows.append(row)
        else:
            rows = self.tracker.by_status("scored", "tailored", min_score=cfg.min_score)
        rows = rows[: (limit or cfg.max_applications_per_run)]
        answers = cfg.real_answers()
        for row in rows:
            key = row["key"]
            job = self.tracker.posting(key)
            try:
                if not row["resume_path"] or not Path(row["resume_path"]).exists():
                    log.info("tailoring resume for %s — %s", job.company, job.title)
                    resume_path, cover_path = self.tailor(key)
                else:
                    resume_path, cover_path = Path(row["resume_path"]), Path(row["cover_letter_path"])
                log.info("applying: %s — %s (%s)", job.company, job.title, job.apply_url or job.url)
                res = apply_to_job(self.llm, job, profile, resume_path, cover_path, answers,
                                   auto_submit=auto, headless=cfg.headless, screenshot_dir=resume_path.parent)
            except Exception as e:  # noqa: BLE001 - one bad job must not stop the whole run
                log.warning("  -> failed: %s", e)
                self.tracker.update(key, status="failed", notes=str(e)[:500])
                continue
            self.tracker.update(key, status=res.status, notes=res.notes)
            log.info("  -> %s: %s", res.status, res.notes)

    # ---------------------------------------------------------------- run --
    def run(self, resume_file: str | None, auto_submit: bool | None = None) -> None:
        if resume_file:
            p = self.analyze(resume_file)
            log.info("profile: %s — %s (%.0f yrs)", p.contact.full_name, p.headline, p.years_of_experience)
        self.search()
        self.score()
        self.apply(auto_submit=auto_submit)
