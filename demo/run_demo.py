"""Offline end-to-end demo with random data - no API key and no internet needed.

What is REAL here: resume text extraction, prefiltering, ranking, SQLite tracking, tailoring flow,
DOCX rendering, the Playwright browser form filling, the required-field safety gate and submission.
What is SIMULATED: Claude (a rule-based MockLLM below), the job boards (random postings), and the
company application sites (a local HTTP server that mimics Lever / Greenhouse forms).

    python demo/run_demo.py [--seed 7] [--jobs 30] [--out /tmp/jobagent-demo]
"""
from __future__ import annotations

import argparse
import email.parser
import email.policy
import json
import os
import random
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from glob import glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobagent import sources  # noqa: E402
from jobagent.config import Settings  # noqa: E402
from jobagent.models import (  # noqa: E402
    CandidateProfile, ContactInfo, CoverLetter, EducationItem, ExperienceItem, FieldAnswer, FitAssessment,
    FormAnswers, JobPosting, ProjectItem, TailoredResume, VerificationResult,
)

# ------------------------------------------------------------------------------ random candidate --
FIRST = ["Aarav", "Meera", "Rohan", "Kavya", "Arjun", "Ishita", "Vikram", "Ananya"]
LAST = ["Sharma", "Iyer", "Kulkarni", "Reddy", "Menon", "Patil", "Bose", "Nair"]
SKILL_POOL = ["Python", "Django", "FastAPI", "PostgreSQL", "Redis", "Docker", "Kubernetes", "AWS", "Kafka",
              "REST APIs", "GraphQL", "Celery", "CI/CD", "Terraform", "Go", "SQL", "Linux", "pytest"]


def make_candidate(rng: random.Random) -> dict:
    name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    skills = rng.sample(SKILL_POOL, 10)
    return {
        "name": name,
        "email": name.lower().replace(" ", ".") + "@example.com",
        "phone": f"+91 9{rng.randint(100000000, 999999999)}",
        "location": "Bengaluru, India",
        "linkedin": "linkedin.com/in/" + name.lower().replace(" ", "-"),
        "github": "github.com/" + name.split()[0].lower() + str(rng.randint(10, 99)),
        "skills": skills,
        "jobs": [
            ("Software Engineer II", "Finlytics", "2023", "Present",
             [f"Built {skills[0]} microservices handling {rng.randint(2, 9)}M requests/day",
              f"Cut p95 API latency by {rng.randint(20, 60)}% using {skills[3]} caching",
              f"Led migration of {rng.randint(5, 15)} services to {skills[5]}"]),
            ("Software Engineer", "CartNest", "2021", "2023",
             [f"Designed {skills[1]} REST APIs for the checkout flow",
              f"Raised test coverage from {rng.randint(30, 50)}% to {rng.randint(80, 92)}% with {skills[2]}"]),
        ],
        "edu": ("B.E.", "Computer Science", "RV College of Engineering", "2017", "2021"),
    }


def write_resume_docx(c: dict, path: Path) -> None:
    import docx
    d = docx.Document()
    d.add_heading(c["name"], 0)
    d.add_paragraph(f'{c["email"]} | {c["phone"]} | {c["location"]} | {c["linkedin"]} | {c["github"]}')
    d.add_heading("Summary", 1)
    d.add_paragraph("Backend engineer with 4+ years building high-traffic Python services and APIs.")
    d.add_heading("Skills", 1)
    d.add_paragraph(", ".join(c["skills"]))
    d.add_heading("Experience", 1)
    for title, co, s, e, bullets in c["jobs"]:
        d.add_paragraph(f"{title}, {co} ({s} - {e})")
        for b in bullets:
            d.add_paragraph(b, style="List Bullet")
    d.add_heading("Education", 1)
    deg, field, inst, s, e = c["edu"]
    d.add_paragraph(f"{deg} {field}, {inst} ({s} - {e})")
    d.save(str(path))


# ----------------------------------------------------------------------------------- random jobs --
COMPANIES = ["Zentrix", "Orbitly", "Quantia", "Nimbusly", "Paylane", "Hexaware Labs", "Brightloop", "Kiteform",
             "Vantor", "Solace AI", "Tesserly", "Mudra Pay"]
GOOD_TITLES = ["Backend Engineer", "Senior Backend Engineer", "Python Developer", "Software Engineer - Platform",
               "Backend Developer (Python)", "Software Engineer II", "Data Engineer"]
BAD_TITLES = ["Sales Manager", "Graphic Designer", "Staff Nurse", "Account Executive", "HR Business Partner"]
LOCS = ["Bengaluru, India", "Remote", "Hyderabad, India", "Berlin, Germany", "Remote - US only", "New York, NY"]


def make_jobs(rng: random.Random, n: int, port: int) -> list[JobPosting]:
    jobs = []
    now = datetime.now(timezone.utc)
    for i in range(n):
        title = rng.choice(GOOD_TITLES) if rng.random() < 0.75 else rng.choice(BAD_TITLES)
        company = rng.choice(COMPANIES)
        loc = rng.choice(LOCS)
        req = rng.sample(SKILL_POOL, 5)
        extra = []
        if rng.random() < 0.12:
            extra.append("Active US security clearance required.")
        if rng.random() < 0.12:
            extra.append("10+ years of professional experience required.")
        desc = (f"{company} is hiring a {title}.\nRequirements:\n- " + "\n- ".join(req) +
                "\n- 3+ years building production services\n" + "\n".join(extra))
        source = rng.choice(["greenhouse", "greenhouse", "lever", "lever", "remotive"])
        slug = re.sub(r"[^a-z]", "", company.lower())
        jid = str(1000 + i)
        if source == "lever":
            url = f"http://127.0.0.1:{port}/lever/{slug}/{jid}"
            apply_url = url  # job page first, then "Apply for this job"
        elif source == "greenhouse":
            url = apply_url = f"http://127.0.0.1:{port}/greenhouse/{slug}/{jid}"
        else:
            url = apply_url = f"https://remotive.example/{jid}"
        jobs.append(JobPosting(
            source=source, source_id=f"{slug}/{jid}", company=company, title=title, location=loc,
            url=url, apply_url=apply_url, description=desc, remote="remote" in loc.lower(),
            posted_at=(now - timedelta(days=rng.randint(1, 80))).isoformat(),
        ))
    return jobs


# -------------------------------------------------------------------------------- fake job sites --
LEVER_JOB = """<html><body><h2>{title}</h2><p>{company}</p>
<a href="#" id="li">Apply with LinkedIn</a> <a href="{path}/apply">Apply for this job</a></body></html>"""
LEVER_FORM = """<html><body><h3>Submit your application</h3>
<form method="post" action="/submit/lever/{slug}/{jid}" enctype="multipart/form-data">
<label>Resume/CV * <input type="file" name="resume" required></label>
<label>Full name * <input name="name" required></label>
<label>Email * <input name="email" type="email" required></label>
<label>Phone <input name="phone"></label>
<label>Current company <input name="org"></label>
<label>LinkedIn URL <input name="urls[LinkedIn]"></label>
<label>GitHub URL <input name="urls[GitHub]"></label>
<label>Additional information <textarea name="comments"></textarea></label>
<button type="submit" id="btn-submit">Submit application</button></form></body></html>"""
GH_FORM = """<html><body><h1>{title} at {company}</h1>
<form method="post" action="/submit/greenhouse/{slug}/{jid}" enctype="multipart/form-data">
<label for="first_name">First Name*</label><input id="first_name" name="first_name" required>
<label for="last_name">Last Name*</label><input id="last_name" name="last_name" required>
<label for="email">Email*</label><input id="email" name="email" required>
<label for="phone">Phone</label><input id="phone" name="phone">
<label for="resume">Resume/CV*</label><input type="file" id="resume" name="resume" required style="display:none">
<label for="cover_letter">Cover Letter</label><input type="file" id="cover_letter" name="cover_letter" style="display:none">
<label id="country-label" for="country">Country*</label>
<input id="country" name="country" role="combobox" aria-labelledby="country-label" required
  onfocus="document.getElementById('lb').style.display='block'">
<div id="lb" role="listbox" style="display:none">
 <div role="option" onclick="country.value='India';lb.style.display='none'">India</div>
 <div role="option" onclick="country.value='United States';lb.style.display='none'">United States</div>
 <div role="option" onclick="country.value='Germany';lb.style.display='none'">Germany</div></div>
<fieldset><legend>Will you now or in the future require visa sponsorship?*</legend>
 <input type="radio" id="sp_y" name="sponsorship" value="Yes" required><label for="sp_y">Yes</label>
 <input type="radio" id="sp_n" name="sponsorship" value="No"><label for="sp_n">No</label></fieldset>
<label for="notice">What is your notice period?</label><input id="notice" name="notice">
<label for="why">Why do you want to work at {company}?</label><textarea id="why" name="why"></textarea>
<label for="gender">Gender</label><select id="gender" name="gender"><option></option><option>Male</option>
 <option>Female</option><option>Non-binary</option><option>Decline to self-identify</option></select>
{extra}
<fieldset><legend>Privacy*</legend><input type="checkbox" id="consent" name="consent" required>
 <label for="consent">I agree to the privacy policy</label></fieldset>
<button type="submit">Submit application</button></form></body></html>"""
# Every 3rd Greenhouse form asks something no resume can answer -> the safety gate must stop auto-submit.
GH_EXTRA = '<label for="clr">Security clearance ID number*</label><input id="clr" name="clr" required>'

SUBMISSIONS: list[dict] = []
JOBS_BY_PATH: dict[str, JobPosting] = {}


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, body: str, code: int = 200):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        m = re.match(r"^/(lever|greenhouse)/([a-z]+)/(\d+)(/apply)?$", self.path)
        if not m:
            return self._send("not found", 404)
        kind, slug, jid, apply = m.groups()
        job = JOBS_BY_PATH.get(f"{slug}/{jid}")
        if not job:
            return self._send("not found", 404)
        fmt = dict(title=job.title, company=job.company, slug=slug, jid=jid, path=f"/{kind}/{slug}/{jid}")
        if kind == "lever":
            return self._send((LEVER_FORM if apply else LEVER_JOB).format(**fmt))
        return self._send(GH_FORM.format(extra=GH_EXTRA if int(jid) % 3 == 0 else "", **fmt))

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(
            f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body)
        fields, files = {}, {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if part.get_filename():
                files[name] = (part.get_filename(), len(part.get_payload(decode=True) or b""))
            else:
                fields[name] = part.get_content().strip()
        SUBMISSIONS.append({"path": self.path, "fields": fields, "files": files})
        self._send("<html><body><h2>Thank you for applying!</h2></body></html>")


# ---------------------------------------------------------------------------------- mock Claude --
class MockLLM:
    """Stands in for Claude with simple, deterministic rules. Same interface as jobagent.llm.LLM."""

    def __init__(self, candidate: dict, profile: CandidateProfile):
        self.c, self.p = candidate, profile
        self.calls: dict[str, int] = {}
        self._flagged_once = False

    def web_search_text(self, prompt, allowed_domains, max_uses=8):
        self.calls["web_search"] = self.calls.get("web_search", 0) + 1
        return "https://job-boards.greenhouse.io/zentrix/jobs/1\nhttps://jobs.lever.co/orbitly/abc"

    def parse(self, *, system, user, schema, **_):
        self.calls[schema.__name__] = self.calls.get(schema.__name__, 0) + 1
        return getattr(self, "_" + schema.__name__)(system, user)

    def _CandidateProfile(self, system, user):
        assert self.c["name"] in user, "resume text should reach the model"
        return self.p

    def _FitAssessment(self, system, user):
        job = user.split("<job>")[1]
        req = re.findall(r"^- (.+)$", job, re.M)
        have = {s.lower() for s in self.p.skills}
        matched = [r for r in req if r.lower() in have]
        missing = [r for r in req if r.lower() not in have and "years" not in r]
        breakers = []
        if "clearance" in job.lower():
            breakers.append("Requires US security clearance")
        if "10+ years" in job:
            breakers.append("Requires 10+ years (candidate has ~4)")
        if "us only" in job.lower():
            breakers.append("Remote role restricted to the US")
        title = job.split("Title:")[1].split("\n")[0].lower()
        related = any(w in title for w in ("backend", "python", "software", "data"))
        score = int(45 + 50 * len(matched) / max(1, len(matched) + len(missing))) if related else 15
        if breakers:
            score = min(score, 40)
        rec = "apply" if score >= 70 else ("maybe" if score >= 55 else "skip")
        return FitAssessment(score=score, recommendation=rec, matched_requirements=matched,
                             missing_requirements=missing, dealbreakers=breakers,
                             reasoning=f"{len(matched)}/{len(matched) + len(missing)} listed skills matched.",
                             keywords_to_emphasize=matched)

    def _TailoredResume(self, system, user):
        job = user.split("<job>")[1].lower()
        skills = sorted(self.p.skills, key=lambda s: s.lower() not in job)  # job-relevant skills first
        title = user.split("Title:")[1].split("\n")[0].strip()
        exp = [e.model_copy(update={"bullets": sorted(e.bullets, key=lambda b: not any(
            s.lower() in b.lower() and s.lower() in job for s in skills))}) for e in self.p.experience]
        summary = f"Backend engineer targeting {title} roles; strongest in {', '.join(skills[:3])}."
        if "fact-checker found" not in user and not self._flagged_once:
            summary += " Certified Kubernetes Administrator."  # an invented claim, to exercise the fact-check
        return TailoredResume(contact=ContactInfo(full_name="WRONG NAME"),  # pipeline must restore contact
                              headline=title, summary=summary, skills=skills, experience=exp,
                              education=self.p.education, change_log=["Reordered skills and bullets for the role"])

    def _VerificationResult(self, system, user):
        if "Certified Kubernetes Administrator" in user:
            self._flagged_once = True
            return VerificationResult(faithful=False, unsupported_claims=["Certified Kubernetes Administrator"])
        return VerificationResult(faithful=True, unsupported_claims=[])

    def _CoverLetter(self, system, user):
        company = user.split("Company:")[1].split("\n")[0].strip()
        title = user.split("Title:")[1].split("\n")[0].strip()
        b = self.c["jobs"][0][4]
        return CoverLetter(body=f"Dear {company} hiring team,\n\nI'm applying for the {title} role. "
                                f"At Finlytics I {b[0][0].lower() + b[0][1:]} and {b[1][0].lower() + b[1][1:]}.\n\n"
                                f"I'd welcome the chance to bring that experience to {company}.\n\n"
                                f"Best regards,\n{self.c['name']}")

    def _FormAnswers(self, system, user):
        fields = json.loads(user.split("<form_fields>")[1].split("</form_fields>")[0])
        std = json.loads(user.split("<standard_answers>")[1].split("</standard_answers>")[0])
        company = user.split("<job>")[1].split("—")[0].strip()
        first, last = self.c["name"].split()
        rules = [("first name", first), ("last name", last), ("full name", self.c["name"]),
                 ("email", self.c["email"]), ("phone", self.c["phone"]), ("linkedin", self.c["linkedin"]),
                 ("github", self.c["github"]), ("current company", "Finlytics"),
                 ("country", "India"), ("sponsorship", std.get("requires_sponsorship")),
                 ("notice", std.get("notice_period")), ("gender", std.get("gender")),
                 ("privacy", std.get("accept_privacy_policy")),
                 ("why do you want", f"I build high-traffic Python APIs and {company}'s platform work fits that."),
                 ("additional information", "Cover letter:\n" + user.split("<cover_letter>")[1].split("</cover_letter>")[0].strip())]
        answers, needs = [], []
        for f in fields:
            label = f["label"].lower()
            val = next((v for k, v in rules if k in label), None)
            if f["type"] in ("radio", "checkbox", "select", "combobox") and val and f["options"]:
                opt = next((o for o in f["options"] if o.lower() == str(val).lower()), None)
                if f["type"] == "checkbox" and str(val).lower() in ("yes", "true"):
                    opt = f["options"][0]
                val = opt
            if val:
                answers.append(FieldAnswer(key=f["key"], value=val, confidence="high"))
            else:
                needs.append(f["key"])
        return FormAnswers(answers=answers, needs_human=needs)


# ------------------------------------------------------------------------------------------ main --
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--jobs", type=int, default=30)
    ap.add_argument("--out", default="demo_output")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for f in ("data/jobs.db",):
        (out / f).unlink(missing_ok=True)

    if not os.environ.get("JOBAGENT_CHROME"):
        bundled = sorted(glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
        if bundled:
            os.environ["JOBAGENT_CHROME"] = bundled[-1]
    os.environ.setdefault("ANTHROPIC_API_KEY", "demo-not-used")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # --- random data
    cand = make_candidate(rng)
    resume_file = out / "sample_resume.docx"
    write_resume_docx(cand, resume_file)
    jobs = make_jobs(rng, args.jobs, port)
    for j in jobs:
        JOBS_BY_PATH[j.source_id] = j
    profile = CandidateProfile(
        contact=ContactInfo(full_name=cand["name"], email=cand["email"], phone=cand["phone"],
                            location=cand["location"], linkedin=cand["linkedin"], github=cand["github"]),
        headline="Backend Engineer (Python)", summary="Backend engineer with 4+ years of Python services.",
        years_of_experience=4, seniority="mid", skills=cand["skills"],
        experience=[ExperienceItem(company=co, title=t, start=s, end=e, bullets=b) for t, co, s, e, b in cand["jobs"]],
        education=[EducationItem(institution=cand["edu"][2], degree=cand["edu"][0], field=cand["edu"][1],
                                 start=cand["edu"][3], end=cand["edu"][4])],
        projects=[ProjectItem(name="ratelimit-py", description="Open-source Redis rate limiter")],
        target_titles=["Backend Engineer", "Python Developer", "Software Engineer"],
        search_keywords=["python", "backend"], strengths=["High-traffic API design"],
        gaps=["No public cloud certification"], resume_improvements=["Add a project section with links"],
    )

    # --- point the job sources at the random postings
    sources.fetch_greenhouse = lambda b: [j for j in jobs if j.source == "greenhouse"] if b == "zentrix" else []
    sources.fetch_lever = lambda c: [j for j in jobs if j.source == "lever"] if c == "orbitly" else []
    sources.fetch_remotive = lambda q, limit=50: [j for j in jobs if j.source == "remotive"]

    settings = Settings.model_validate({
        "data_dir": str(out / "data"), "output_dir": str(out / "applications"),
        "search": {"locations": ["Bengaluru", "Hyderabad", "India"], "remote_ok": True,
                   "max_jobs_to_score": 25, "max_age_days": 60},
        "apply": {"min_score": 70, "max_applications_per_run": 6, "auto_submit": True, "headless": True,
                  "standard_answers": {"requires_sponsorship": "No", "notice_period": "30 days",
                                       "gender": "Decline to self-identify", "accept_privacy_policy": "Yes",
                                       "expected_salary": "TODO"}},
    })
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.output_dir).mkdir(parents=True, exist_ok=True)

    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    for noisy in ("httpx", "anthropic", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    from jobagent.cli import _print_profile, _print_status
    from jobagent.pipeline import Agent

    agent = Agent(settings)
    agent.llm = MockLLM(cand, profile)

    print(f"\n### 1. ANALYZE  (resume: {resume_file})")
    _print_profile(agent.analyze(resume_file))
    print(f"\n### 2. SEARCH  ({len(jobs)} random postings across Greenhouse / Lever / Remotive)")
    new = agent.search()
    print(f"kept {len(new)} of {len(jobs)} after prefilter (title / location / age / exclusions)")
    print("\n### 3. SCORE")
    agent.score()
    print("\n### 4+5. TAILOR + APPLY  (auto-submit, headless browser, local fake company sites)")
    agent.apply()
    print("\n### STATUS")
    _print_status(agent.tracker)

    print(f"\n### WHAT THE FAKE COMPANY SITES RECEIVED ({len(SUBMISSIONS)} submissions)")
    for s in SUBMISSIONS:
        print(f"- {s['path']}")
        print(f"    files : {s['files']}")
        shown = {k: (v[:60] + '…' if len(v) > 60 else v) for k, v in s["fields"].items()}
        print(f"    fields: {shown}")
    print(f"\nMock-Claude calls: {agent.llm.calls}")
    print(f"Outputs: {out}")
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
