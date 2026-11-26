"""Configuration loading (config.yaml)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


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


class ApplyConfig(BaseModel):
    min_score: int = 70
    max_applications_per_run: int = 10
    auto_submit: bool = False  # False = fill the form, then wait for you to review & submit
    headless: bool = False
    # Truthful answers to questions the resume can't answer. Never guessed by the agent.
    standard_answers: dict[str, str] = Field(default_factory=dict)


class Settings(BaseModel):
    model: str = "claude-opus-5-5"
    data_dir: str = "data"
    output_dir: str = "output"
    search: SearchConfig = Field(default_factory=SearchConfig)
    apply: ApplyConfig = Field(default_factory=ApplyConfig)

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
