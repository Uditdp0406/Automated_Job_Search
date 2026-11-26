"""Pydantic schemas shared across the pipeline (also used as Claude structured-output formats)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- resume ----
class ExperienceItem(BaseModel):
    company: str
    title: str
    location: str = ""
    start: str = ""
    end: str = ""
    bullets: list[str] = Field(default_factory=list)


class EducationItem(BaseModel):
    institution: str
    degree: str = ""
    field: str = ""
    start: str = ""
    end: str = ""
    details: list[str] = Field(default_factory=list)


class ProjectItem(BaseModel):
    name: str
    description: str = ""
    bullets: list[str] = Field(default_factory=list)
    link: str = ""


class ContactInfo(BaseModel):
    full_name: str
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""


class CandidateProfile(BaseModel):
    """Structured version of the candidate's resume plus Claude's analysis of it."""

    contact: ContactInfo
    headline: str = Field(description="One-line professional headline")
    summary: str = Field(description="Professional summary as written / implied by the resume")
    years_of_experience: float
    seniority: Literal["intern", "entry", "mid", "senior", "staff", "principal", "manager", "director", "executive"]
    skills: list[str]
    experience: list[ExperienceItem]
    education: list[EducationItem]
    projects: list[ProjectItem] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    # analysis
    target_titles: list[str] = Field(description="5-10 job titles this person is a strong fit for")
    search_keywords: list[str] = Field(description="Keywords to use when searching job boards")
    strengths: list[str]
    gaps: list[str] = Field(description="Weaknesses or missing items a recruiter might notice")
    resume_improvements: list[str] = Field(description="Concrete, general suggestions to improve the resume")


# ------------------------------------------------------------------ jobs ----
class JobPosting(BaseModel):
    source: str  # greenhouse | lever | remotive | ...
    source_id: str
    company: str
    title: str
    location: str = ""
    url: str
    apply_url: str = ""
    description: str = ""
    remote: bool = False
    posted_at: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    @property
    def can_auto_apply(self) -> bool:
        return self.source in ("greenhouse", "lever")


class FitAssessment(BaseModel):
    score: int = Field(description="0-100 overall fit")
    recommendation: Literal["apply", "maybe", "skip"]
    matched_requirements: list[str]
    missing_requirements: list[str]
    dealbreakers: list[str] = Field(description="Hard blockers, e.g. required clearance, location, years")
    reasoning: str
    keywords_to_emphasize: list[str] = Field(description="Job keywords the candidate genuinely has and should surface")


# --------------------------------------------------------------- tailor ----
class TailoredResume(BaseModel):
    contact: ContactInfo
    headline: str
    summary: str
    skills: list[str]
    experience: list[ExperienceItem]
    education: list[EducationItem]
    projects: list[ProjectItem] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    change_log: list[str] = Field(description="What was changed vs. the original and why")


class VerificationResult(BaseModel):
    faithful: bool = Field(description="True if every claim is supported by the original resume")
    unsupported_claims: list[str]


class CoverLetter(BaseModel):
    body: str = Field(description="Cover letter text, plain paragraphs, no placeholders")


# ---------------------------------------------------------------- forms ----
class FieldAnswer(BaseModel):
    key: str
    value: str = Field(description="Text to type, or the exact option label to choose")
    confidence: Literal["high", "medium", "low"]


class FormAnswers(BaseModel):
    answers: list[FieldAnswer]
    needs_human: list[str] = Field(description="Keys of fields you could not answer truthfully from the data given")
    notes: Optional[str] = None
