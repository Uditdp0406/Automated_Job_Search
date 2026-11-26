"""Step 1: read the resume file and analyze it into a CandidateProfile."""
from __future__ import annotations

from pathlib import Path

from .llm import LLM
from .models import CandidateProfile


def extract_text(path: str | Path) -> str:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        text = "\n".join((page.extract_text() or "") for page in PdfReader(str(p)).pages)
    elif suffix == ".docx":
        import docx

        d = docx.Document(str(p))
        parts = [para.text for para in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        text = "\n".join(parts)
    elif suffix in (".txt", ".md"):
        text = p.read_text(encoding="utf-8", errors="replace")
    else:
        raise ValueError(f"Unsupported resume format: {suffix} (use PDF, DOCX, TXT or MD)")
    text = text.strip()
    if len(text) < 200:
        raise ValueError("Very little text extracted - is the resume a scanned image? Export it as text-based PDF/DOCX.")
    return text


ANALYZE_SYSTEM = """You are an expert technical recruiter and career coach.
You turn a raw resume into a precise structured profile and give an honest analysis.
Rules:
- Copy facts exactly as stated (companies, titles, dates, numbers). Never invent anything.
- Leave a field empty if the resume does not state it.
- target_titles and search_keywords should be realistic for the candidate's actual level and domain.
- gaps and resume_improvements must be specific and actionable, not generic."""


def analyze_resume(llm: LLM, resume_text: str) -> CandidateProfile:
    return llm.parse(
        system=ANALYZE_SYSTEM,
        user=f"<resume>\n{resume_text}\n</resume>\n\nProduce the structured candidate profile.",
        schema=CandidateProfile,
        effort="high",
    )
