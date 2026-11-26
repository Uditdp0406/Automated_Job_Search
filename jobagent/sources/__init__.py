"""Job sources. Only public, documented job-board APIs are used (no scraping of sites that forbid it)."""
from __future__ import annotations

import html
import logging
import re

import httpx

from ..models import JobPosting

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "jobagent/0.1 (personal job search tool)"}
_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<\s*(br|/p|/li|/h\d)\s*/?>", "\n", s, flags=re.I)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def http_get_json(url: str, params: dict | None = None):
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r.json()


# -------------------------------------------------------------- Greenhouse --
def fetch_greenhouse(board: str) -> list[JobPosting]:
    """Public Greenhouse Job Board API: https://developers.greenhouse.io/job-board.html"""
    try:
        data = http_get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", {"content": "true"})
    except httpx.HTTPError as e:
        log.warning("greenhouse board %s: %s", board, e)
        return []
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        jobs.append(JobPosting(
            source="greenhouse",
            source_id=f"{board}/{j['id']}",
            company=j.get("company_name") or board,
            title=j.get("title", ""),
            location=loc,
            url=j.get("absolute_url", ""),
            apply_url=j.get("absolute_url", ""),
            description=html_to_text(j.get("content", "")),
            remote="remote" in loc.lower(),
            posted_at=j.get("updated_at", ""),
        ))
    return jobs


# ------------------------------------------------------------------- Lever --
def fetch_lever(company: str) -> list[JobPosting]:
    """Public Lever Postings API: https://github.com/lever/postings-api"""
    try:
        data = http_get_json(f"https://api.lever.co/v0/postings/{company}", {"mode": "json"})
    except httpx.HTTPError as e:
        log.warning("lever company %s: %s", company, e)
        return []
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ", ".join(cats.get("allLocations", []) or [])
        parts = [j.get("descriptionPlain", "")]
        for lst in j.get("lists", []):
            parts.append(lst.get("text", ""))
            parts.append(html_to_text(lst.get("content", "")))
        parts.append(j.get("additionalPlain", ""))
        jobs.append(JobPosting(
            source="lever",
            source_id=f"{company}/{j['id']}",
            company=company,
            title=j.get("text", ""),
            location=loc,
            url=j.get("hostedUrl", ""),
            apply_url=j.get("applyUrl", "") or (j.get("hostedUrl", "") + "/apply"),
            description="\n".join(p for p in parts if p),
            remote=(j.get("workplaceType") == "remote") or "remote" in loc.lower(),
            posted_at=str(j.get("createdAt", "")),
        ))
    return jobs


# ---------------------------------------------------------------- Remotive --
def fetch_remotive(search: str, limit: int = 50) -> list[JobPosting]:
    """Remotive public API (remote jobs). Applications happen on external sites -> manual apply."""
    try:
        data = http_get_json("https://remotive.com/api/remote-jobs", {"search": search, "limit": limit})
    except httpx.HTTPError as e:
        log.warning("remotive %r: %s", search, e)
        return []
    return [
        JobPosting(
            source="remotive",
            source_id=str(j["id"]),
            company=j.get("company_name", ""),
            title=j.get("title", ""),
            location=j.get("candidate_required_location", ""),
            url=j.get("url", ""),
            apply_url=j.get("url", ""),
            description=html_to_text(j.get("description", "")),
            remote=True,
            posted_at=j.get("publication_date", ""),
        )
        for j in data.get("jobs", [])
    ]


# ------------------------------------------------- discovery via web search --
GH_RE = re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")
LEVER_RE = re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)")


def discover_boards(llm, titles: list[str], locations: list[str], max_uses: int = 6) -> tuple[set[str], set[str]]:
    """Use Claude's web search to find companies hiring for these titles on Greenhouse / Lever."""
    loc = ", ".join(locations) if locations else "anywhere / remote"
    prompt = (
        "Find current job postings for these roles: " + "; ".join(titles[:6]) +
        f". Location preference: {loc}. Search only Greenhouse and Lever job boards and list every "
        "posting URL you find (one per line)."
    )
    text = llm.web_search_text(
        prompt,
        allowed_domains=["boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co"],
        max_uses=max_uses,
    )
    skip = {"embed", "v1", "jobs"}
    gh = {m.lower() for m in GH_RE.findall(text) if m.lower() not in skip}
    lv = {m.lower() for m in LEVER_RE.findall(text)}
    return gh, lv
