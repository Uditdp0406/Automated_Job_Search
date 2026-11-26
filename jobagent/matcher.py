"""Step 2b: cheap rule-based prefilter, then Claude scores fit for each remaining job."""
from __future__ import annotations

import re

from .config import SearchConfig
from .llm import LLM
from .models import CandidateProfile, FitAssessment, JobPosting

MAX_DESC_CHARS = 12000


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.]+", s.lower()))


def prefilter(job: JobPosting, profile: CandidateProfile, cfg: SearchConfig) -> tuple[bool, str]:
    """Return (keep, reason). Deliberately loose - Claude does the real judgement."""
    title = job.title.lower()
    text = f"{job.title} {job.description}".lower()
    if any(c.lower() == job.company.lower() for c in cfg.exclude_companies):
        return False, "excluded company"
    if any(k.lower() in text for k in cfg.exclude_keywords):
        return False, "excluded keyword"
    if cfg.locations:
        loc = job.location.lower()
        loc_ok = any(l.lower() in loc for l in cfg.locations) or (cfg.remote_ok and (job.remote or "remote" in loc))
        if not loc_ok:
            return False, f"location '{job.location}'"
    titles = cfg.titles + profile.target_titles
    title_words = set().union(*(_words(t) for t in titles)) - {"and", "of", "the", "i", "ii", "iii", "-", "&"}
    if title_words and not (_words(title) & title_words):
        return False, "title not related"
    return True, ""


MATCH_SYSTEM_TMPL = """You are a meticulous recruiter screening jobs for one specific candidate.
Score how well the candidate fits each job (0-100) using only the facts in their profile.
Scoring guide: 85+ = strong fit, most must-haves met; 70-84 = good fit, minor gaps;
50-69 = stretch; <50 = poor fit. Treat explicit hard requirements (clearance, citizenship,
licenses, minimum years far above the candidate's, on-site in a location they excluded) as dealbreakers.
Be honest - the candidate wastes time on poor-fit applications.

<candidate_profile>
{profile}
</candidate_profile>

<candidate_preferences>
{prefs}
</candidate_preferences>"""


class Matcher:
    def __init__(self, llm: LLM, profile: CandidateProfile, cfg: SearchConfig):
        self.llm = llm
        prefs = {"locations": cfg.locations or ["any"], "remote_ok": cfg.remote_ok, "titles": cfg.titles}
        # Same system prompt for every job -> prompt-cached after the first call.
        self.system = MATCH_SYSTEM_TMPL.format(profile=profile.model_dump_json(indent=1), prefs=prefs)

    def assess(self, job: JobPosting) -> FitAssessment:
        user = (
            f"<job>\nCompany: {job.company}\nTitle: {job.title}\nLocation: {job.location}"
            f"{' (remote)' if job.remote else ''}\n\n{job.description[:MAX_DESC_CHARS]}\n</job>\n\nAssess fit."
        )
        return self.llm.parse(system=self.system, user=user, schema=FitAssessment, effort="low", max_tokens=8000)
