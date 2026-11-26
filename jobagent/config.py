"""Configuration loading (config.yaml)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from .notify import NotifyConfig


class SearchConfig(BaseModel):
    titles: list[str] = Field(default_factory=list, description="Extra titles; resume-derived ones are added")
    keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list, description="Accepted locations; empty = any")
    remote_ok: bool = True
    exclude_keywords: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)
    greenhouse_boards: list[str] = Field(default_factory=list, description="Greenhouse board tokens, e.g. 'stripe'")
    lever_companies: list[str] = Field(default_factory=list, description="Lever company slugs, e.g. 'netflix'")
    use_remotive: bool = True
    use_web_discovery: bool = True
    max_jobs_to_score: int = 40
    max_age_days: int = 45  # ignore postings older than this (when the source gives a date); 0 = off
    scoring_workers: int = 4  # parallel Claude scoring calls


class ApplyConfig(BaseModel):
    min_score: int = 70
    max_applications_per_run: int = 10
    auto_submit: bool = False  # False = fill the form, then wait for you to review & submit
    headless: bool = False
    # Truthful answers to questions the resume can't answer. Never guessed by the agent.
    standard_answers: dict[str, str] = Field(default_factory=dict)

    def real_answers(self) -> dict[str, str]:
        """Drop blank / placeholder values so the agent never 'answers' with a template string."""
        return {k: v for k, v in self.standard_answers.items()
                if isinstance(v, str) and v.strip() and not v.strip().upper().startswith(("TODO", "FILL"))}


class WatchConfig(BaseModel):
    """`jobagent watch`: poll boards continuously, alert on new matches, apply immediately."""
    interval_minutes: float = 10        # how often to check watched company boards (min 2)
    discovery_every_hours: float = 12   # Claude web search for new companies to watch; 0 = off
    remotive_every_hours: float = 6     # Remotive is polled rarely (their API asks for low volume); 0 = off
    include_existing_on_first_run: bool = False  # False: jobs already open when watching starts are ignored...
    fresh_hours: float = 24             # ...except ones posted within this many hours (when the date is known)
    auto_apply: bool = True             # tailor + apply the moment a match appears
    auto_submit: bool = True            # False: prepare docs + alert you; you run `apply --job KEY`
    max_applications_per_day: int = 15  # rolling 24h cap, across watch and manual applies
    min_score: Optional[int] = None     # defaults to apply.min_score
    alert_min_score: Optional[int] = None  # alert (without applying) for matches at least this; default = min_score
    poll_workers: int = 8               # boards polled in parallel
    keep_awake: bool = True             # stop the laptop idle-sleeping while watching (lid close still sleeps)


class Settings(BaseModel):
    model: str = "claude-opus-5-5"
    data_dir: str = "data"
    output_dir: str = "output"
    search: SearchConfig = Field(default_factory=SearchConfig)
    apply: ApplyConfig = Field(default_factory=ApplyConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "jobs.db"

    @property
    def profile_path(self) -> Path:
        return Path(self.data_dir) / "profile.json"

    @property
    def resume_text_path(self) -> Path:
        return Path(self.data_dir) / "resume.txt"


def load_settings(path: str | Path | None) -> Settings:
    data: dict[str, Any] = {}
    if path and Path(path).exists():
        data = yaml.safe_load(Path(path).read_text()) or {}
    s = Settings.model_validate(data)
    Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    Path(s.output_dir).mkdir(parents=True, exist_ok=True)
    return s
