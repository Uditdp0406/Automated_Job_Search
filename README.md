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

Then fill in `apply.standard_answers` in `config.yaml` with your real answers. Any value left as `TODO` is
ignored, and the agent flags those questions for you instead of answering them.

Optional:
- Install LibreOffice to also get PDF resumes (`soffice` on PATH); otherwise the DOCX is uploaded.
- `export JOBAGENT_CHROME=/path/to/chrome` to use a Chrome/Chromium you already have instead of
  Playwright's browser.

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
python -m jobagent watch                                      # alert + apply the moment new jobs open
```

Tailored documents go to `output/<company>-<title>-<id>/`: the resume, cover letter, tailored JSON with a
change log, `fact_check.json` (claims the fact-checker couldn't verify against your original resume — the
agent repairs up to twice and warns you if any remain), and screenshots of the filled form.

### Review mode vs. auto-submit

By default (`auto_submit: false`) the agent fills the form in a visible browser window and pauses.
You check it, complete anything it flagged, click **Submit** yourself, then type `y` in the terminal.
With `--auto-submit` (or `auto_submit: true`) it submits on its own **only when every required field
on the page is actually filled** (checked in the browser after filling, not just trusted from Claude);
otherwise it leaves the application as `filled` for you. Start in review mode until you trust
the answers it gives.

### Job statuses

`found` → `scored` (worth applying) / `skipped` (below `min_score` or dealbreaker) → `tailored` →
`filled` (form filled, not submitted) / `applied` / `manual` (external site — apply via the link,
the tailored resume is ready) / `failed`.

## Watch mode: alerts + instant applications when a job opens

```bash
python -m jobagent boards --add greenhouse:stripe --add lever:netflix   # companies to watch (optional)
python -m jobagent notify-test                                          # check your alerts arrive
python -m jobagent watch                                                # runs until Ctrl+C
```

Job boards don't push notifications, so the agent **checks every watched company every few minutes**
(`watch.interval_minutes`, default 10) using the public Greenhouse/Lever APIs. These checks are cheap
and make no Claude calls. When a posting it has never seen appears:

1. **Filter.** The quick title/location/age filter runs (full details are fetched only now, and only
   for relevant postings).
2. **Score.** Claude scores the fit.
3. **🎯 Alert.** Every match goes to your phone, chat or email straight away, before any applying starts.
4. **Tailor.** The resume and cover letter are tailored, with the fact-check.
5. **Apply.** The form is filled and submitted in a headless browser, followed by **✅ Applied**,
   **⚠️ needs you** (a required question it can't answer truthfully) or **📝 apply manually**
   (a site it can't apply on).

Details:
- **First check of each company = baseline.** Jobs that were already open are recorded but not applied
  to (unless posted in the last `fresh_hours`, default 24h). Use `search`/`score`/`apply` for that backlog.
- **The watch list grows.** It holds your `greenhouse_boards`/`lever_companies`, plus
  `boards --add`, plus companies Claude finds by web search every `discovery_every_hours`.
  Boards that no longer exist are dropped automatically. See them with `python -m jobagent boards`.
- **Guardrails.** There's a `max_applications_per_day` cap (rolling 24h, shared with `apply`), the
  required-field safety check, and alerts at most hourly if something keeps failing. A lock file
  stops two watchers running at once.
- **Review mode:** `watch --review` (or `watch.auto_submit: false`) prepares everything and alerts you
  to finish with `python -m jobagent apply --job KEY`.

### Alert channels (`notify:` in config.yaml)

| Channel | Setup |
|---|---|
| **Phone push (easiest)** | Install the free **ntfy** app, subscribe to a long random topic name, set `notify.ntfy_topic` to it |
| Telegram | Create a bot with @BotFather; set env `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; `notify.telegram: true` |
| Slack / Discord | Create an incoming webhook; env `JOBAGENT_WEBHOOK_URL`; `notify.webhook: true` |
| Email | `notify.email.*` + env `SMTP_PASSWORD` (Gmail: use an app password) |
| Desktop / console | On by default (Linux `notify-send`, macOS notifications) |

### Keeping it running 24/7

The watcher has to run on a machine that stays on (your PC while it's awake, or a small always-on
server or VPS). It needs a desktop or `xvfb` only if you want to watch the browser; submissions run headless.

```bash
# simplest: in the background
nohup python -m jobagent watch >> watch.log 2>&1 &

# or cron: one check every 10 minutes (the state lives in data/jobs.db, the lock prevents overlaps)
*/10 * * * * cd /path/to/Automated_Job_Search && .venv/bin/python -m jobagent watch --once >> watch.log 2>&1
```

Windows: in Task Scheduler, create a task that runs every 10 minutes with the program
`C:\path\to\.venv\Scripts\python.exe`, the arguments `-m jobagent watch --once`, and the start-in folder
set to the project.

## Configuration highlights (`config.yaml`)

| Key | What it does |
|---|---|
| `search.locations`, `search.remote_ok` | Location filter (empty list = anywhere) |
| `search.greenhouse_boards`, `search.lever_companies` | Companies you specifically want to watch |
| `search.use_web_discovery` | Let Claude web-search for more Greenhouse/Lever companies |
| `search.max_jobs_to_score` | Caps Claude scoring calls per run; jobs are ranked locally first so the most relevant get scored |
| `search.max_age_days` | Skip postings older than this (default 45) |
| `search.scoring_workers` | Parallel scoring calls (default 4) |
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
- **Prompt-injection aware:** job descriptions and form labels are treated as untrusted data in every
  prompt. Legal attestation / consent checkboxes are left for you unless `standard_answers` says otherwise.
- **Never clicks "Apply with LinkedIn/Indeed/Google"** or other third-party sign-in flows.
- **Never re-applies** to a job already marked `applied`, even when asked with `--job`.
- **CAPTCHAs are not bypassed.** If a form shows one, the job is left as `filled` for you to finish.
- **Caps** on how many jobs are scored and applied to per run.

## Cost

It uses `claude-opus-5-5` by default (change `model` in config). Rough cost per run: analysis ≈ 1
call; scoring ≈ 1 small call per job (the profile is prompt-cached across jobs); each application
≈ 4 calls (tailor, fact-check, cover letter, form answers). `max_jobs_to_score` and
`max_applications_per_run` keep spending bounded.

## Try it without an API key (offline demo)

```bash
python demo/run_demo.py --seed 21          # outputs go to demo_output/
python demo/run_demo.py --watch --seed 21  # watch mode: new jobs open while the agent is watching
```

Generates a random candidate resume (DOCX) and ~30 random job postings, serves fake Lever and Greenhouse
application sites locally, and runs the whole pipeline in a headless browser. Claude is replaced by a
rule-based stand-in, so this tests the plumbing (search, filtering, tracking, tailoring flow, rendering,
form filling, the safety gate, submission), not the quality of Claude's writing.

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
  watcher.py    watch mode: polling, baseline, alert -> tailor -> apply on new postings
  notify.py     alert channels (ntfy, Telegram, Slack/Discord, email, desktop, console)
  tracker.py    SQLite job tracker (+ watch list, seen postings)
  llm.py        Anthropic SDK wrapper (structured outputs, refusal handling)
  models.py     Pydantic schemas
```
