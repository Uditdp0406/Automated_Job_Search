# Automated Job Search Agent

An AI agent (powered by Claude) that takes your resume and runs your job search end to end:

1. **Analyze** – reads your resume (PDF/DOCX/TXT), extracts a structured profile, and gives honest
   feedback: strengths, gaps, target titles, search keywords, improvement suggestions.
2. **Search** – pulls live postings from public job-board APIs (Greenhouse, Lever, Remotive) and uses
   Claude web search to discover more companies hiring for your target titles.
3. **Score** – a cheap rule-based prefilter (title/location/excluded keywords), then Claude scores each job
   0–100 against your profile, listing matched/missing requirements and dealbreakers.
4. **Tailor** – for each job worth applying to, Claude rewrites your resume for that posting
   (reorders, rewords and emphasizes, but **never invents** facts), runs a separate fact-check pass
   against your original resume, then renders a DOCX (and a PDF if LibreOffice is installed) plus a
   cover letter.
5. **Apply** – opens the Greenhouse/Lever application form in a real browser, uploads the tailored
   resume, reads every field on the form (including custom questions), has Claude answer them from your
   profile, and either submits automatically or waits for you to review and click Submit.

Everything is tracked in SQLite (`data/jobs.db`), so the agent never applies to the same job twice.

```
resume.pdf ─► analyze ─► profile.json
                             │
   Greenhouse / Lever / Remotive / web discovery ─► search ─► prefilter ─► Claude score
                                                                               │ score ≥ min_score
                                              tailored resume + cover letter ◄─┘
                                                             │
                                       browser form fill ─► you review ─► submitted ✔
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium            # browser used to fill application forms
export ANTHROPIC_API_KEY=sk-ant-...     # from https://console.anthropic.com
cp config.example.yaml config.yaml      # then edit locations, companies, standard answers
```

Optional: install LibreOffice to also get PDF resumes (`soffice` on PATH); otherwise DOCX is uploaded.

## Usage

```bash
# Everything in one go
python -m jobagent run --resume ~/Documents/resume.pdf

# Or step by step
python -m jobagent analyze --resume ~/Documents/resume.pdf   # prints profile + feedback
python -m jobagent search                                     # find + prefilter new jobs
python -m jobagent score                                      # Claude scores them
python -m jobagent status                                     # table of jobs, scores, statuses
python -m jobagent tailor "lever:acme/1234-abcd"              # tailored resume + cover letter only
python -m jobagent apply --limit 5                            # tailor + fill forms for top jobs
python -m jobagent apply --job "greenhouse:stripe/5551234"    # one specific job
```

Tailored documents go to `output/<company>-<title>-<id>/` (resume, cover letter, the tailored JSON
with a change log, and screenshots of the filled form).

### Review mode vs. auto-submit

By default (`auto_submit: false`) the agent fills the form in a visible browser window and pauses.
You check it, complete anything it flagged, click **Submit** yourself, then type `y` in the terminal.
With `--auto-submit` (or `auto_submit: true`) it submits on its own **only when every required field
was answered**; otherwise it still leaves the application for you. Start in review mode until you trust
the answers it gives.

### Job statuses

`found` → `scored` (worth applying) / `skipped` (below `min_score` or dealbreaker) → `tailored` →
`filled` (form filled, not submitted) / `applied` / `manual` (external site — apply via the link,
the tailored resume is ready) / `failed`.

## Configuration highlights (`config.yaml`)

| Key | What it does |
|---|---|
| `search.locations`, `search.remote_ok` | Location filter (empty list = anywhere) |
| `search.greenhouse_boards`, `search.lever_companies` | Companies you specifically want to watch |
| `search.use_web_discovery` | Let Claude web-search for more Greenhouse/Lever companies |
| `search.max_jobs_to_score` | Caps Claude scoring calls per run (cost control) |
| `apply.min_score` | Only apply to jobs scored at least this |
| `apply.max_applications_per_run` | Daily/run cap |
| `apply.standard_answers` | Your truthful answers to work authorization, sponsorship, salary, notice period, EEO questions. The agent **only** answers such questions from here and never guesses. |

## Safety and honesty built in

- **No fabrication:** tailoring is limited to facts in your original resume, and a second Claude pass
  fact-checks the tailored version. Anything it can't support is removed before the resume is rendered.
- **No guessing on legal/eligibility questions:** these come only from `standard_answers`; anything else
  is flagged for you.
- **Only public APIs:** Greenhouse Job Board API, Lever Postings API, Remotive API. The agent does not
  scrape LinkedIn/Indeed (their terms forbid automation, and automated applies there can get your account
  banned). Jobs from Remotive are marked `manual`, with tailored docs ready.
- **CAPTCHAs are not bypassed.** If a form shows one, the job is left as `filled` for you to finish.
- **Caps** on how many jobs are scored and applied to per run.

## Cost

It uses `claude-opus-5-5` by default (change `model` in config). Rough cost per run: analysis ≈ 1
call; scoring ≈ 1 small call per job (the profile is prompt-cached across jobs); each application
≈ 4 calls (tailor, fact-check, cover letter, form answers). `max_jobs_to_score` and
`max_applications_per_run` keep spending bounded.

## Development

```bash
pip install pytest
python -m pytest -q      # offline tests: no API calls (includes a browser form-fill test)
```

Project layout:

```
jobagent/
  cli.py        command-line entry point
  pipeline.py   orchestrates analyze → search → score → tailor → apply
  resume.py     resume text extraction + Claude analysis
  sources/      Greenhouse, Lever, Remotive fetchers + web-search discovery
  matcher.py    prefilter + Claude fit scoring
  tailor.py     tailored resume, fact-check, cover letter, DOCX/PDF rendering
  applier.py    Playwright form discovery + Claude form answering + submit
  tracker.py    SQLite job tracker
  llm.py        Anthropic SDK wrapper (structured outputs, refusal handling)
  models.py     Pydantic schemas
```
