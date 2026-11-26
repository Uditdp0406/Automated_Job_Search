"""Step 4: open the application form in a real browser, let Claude fill it, then submit (or hand to you).

Works on Greenhouse and Lever hosted application forms. Fields are discovered generically from the
page (labels, types, options) so per-company custom questions are handled too. Anything Claude
cannot answer truthfully from your resume / standard_answers is left for you.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .llm import LLM
from .models import CandidateProfile, FormAnswers, JobPosting

log = logging.getLogger(__name__)

# Collect every visible form control, tag it with a stable key, and describe it.
EXTRACT_JS = r"""
() => {
  const visible = el => {
    if (el.type === 'file') return true;
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return (r.width > 0 || r.height > 0) && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const labelFor = el => {
    let t = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = l.innerText; }
    if (!t && el.getAttribute('aria-labelledby')) {
      t = el.getAttribute('aria-labelledby').split(' ').map(i => (document.getElementById(i)||{}).innerText||'').join(' ');
    }
    if (!t) t = el.getAttribute('aria-label') || '';
    if (!t) { const l = el.closest('label'); if (l) t = l.innerText; }
    if (!t) { const q = el.closest('.application-question, .field, fieldset, .select__container, div');
              if (q) { const h = q.querySelector('label, legend, .application-label, .text'); if (h) t = h.innerText; } }
    if (!t) t = el.placeholder || el.name || '';
    return t.replace(/\s+/g, ' ').trim().slice(0, 300);
  };
  const out = []; const groups = {}; let n = 0;
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.tagName === 'SELECT') ? 'select' : (el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text'));
    if (['hidden','submit','button','image','reset','search'].includes(type)) continue;
    if (!visible(el)) continue;
    const required = el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(labelFor(el));
    if (type === 'radio' || type === 'checkbox') {
      const gname = el.name || ('g' + n);
      const optLabel = (el.closest('label') ? el.closest('label').innerText : labelFor(el)).trim();
      if (!groups[gname]) {
        const fs = el.closest('fieldset, .application-question, .field, div');
        const legend = fs ? (fs.querySelector('legend, .application-label, label') || {}).innerText || '' : '';
        groups[gname] = {key: 'k' + (n++), type, label: legend.replace(/\s+/g,' ').trim().slice(0,300) || gname,
                         required, options: []};
        out.push(groups[gname]);
      }
      const g = groups[gname];
      el.setAttribute('data-jobagent-key', g.key + '-' + g.options.length);
      g.options.push(optLabel);
      continue;
    }
    const key = 'k' + (n++);
    el.setAttribute('data-jobagent-key', key);
    const f = {key, type, label: labelFor(el), required, name: el.name || '', options: []};
    if (type === 'select') f.options = [...el.options].map(o => o.text.trim()).filter(Boolean);
    if (el.getAttribute('role') === 'combobox') f.type = 'combobox';
    out.push(f);
  }
  return out;
}
"""

FORM_SYSTEM = """You fill out job application forms on behalf of a candidate.
For each field key, give the value to enter. For select/radio/checkbox fields, the value must be
EXACTLY one of the listed option labels (for checkbox groups you may give several separated by ' || ').

Truthfulness rules - these are absolute:
- Use only the candidate profile, the standard answers, the tailored cover letter, and the job.
- Legal/eligibility/compensation/demographic questions (work authorization, sponsorship, salary,
  start date, relocation, gender, race, veteran, disability, criminal history, etc.) may ONLY be
  answered from standard_answers. If the answer isn't there, put the key in needs_human.
- Never claim skills, degrees, or experience the profile doesn't show.
- For short free-text questions ("Why do you want to work here?"), write 2-4 honest sentences
  grounded in the profile and job.
- Skip file-upload fields (they are handled separately) and fields you'd only be guessing at
  (put guesses in needs_human instead)."""


@dataclass
class ApplyResult:
    status: str  # applied | filled | failed | manual
    notes: str = ""


def _answer_form(llm: LLM, fields: list[dict], profile: CandidateProfile, job: JobPosting,
                 cover_letter: str, standard_answers: dict) -> FormAnswers:
    user = (
        f"<candidate_profile>\n{profile.model_dump_json(indent=1)}\n</candidate_profile>\n\n"
        f"<standard_answers>\n{json.dumps(standard_answers, indent=1)}\n</standard_answers>\n\n"
        f"<cover_letter>\n{cover_letter}\n</cover_letter>\n\n"
        f"<job>\n{job.company} — {job.title} ({job.location})\n{job.description[:6000]}\n</job>\n\n"
        f"<form_fields>\n{json.dumps(fields, indent=1)}\n</form_fields>"
    )
    return llm.parse(system=FORM_SYSTEM, user=user, schema=FormAnswers, effort="medium", cache_system=False)


def _fill(page, field: dict, value: str) -> bool:
    key, ftype = field["key"], field["type"]
    try:
        if ftype in ("radio", "checkbox"):
            wanted = [v.strip().lower() for v in value.split("||")]
            ok = False
            for i, opt in enumerate(field["options"]):
                if opt.strip().lower() in wanted:
                    page.locator(f'[data-jobagent-key="{key}-{i}"]').check(force=True)
                    ok = True
            return ok
        loc = page.locator(f'[data-jobagent-key="{key}"]')
        if ftype == "select":
            loc.select_option(label=value)
        elif ftype == "combobox":
            loc.click()
            loc.fill(value)
            page.wait_for_timeout(600)
            page.keyboard.press("Enter")
        else:
            loc.fill(value)
        return True
    except Exception as e:  # noqa: BLE001 - a single bad field must not abort the application
        log.warning("could not fill %s (%s): %s", field.get("label"), ftype, e)
        return False


def _upload_files(page, fields: list[dict], resume: Path, cover_path: Path) -> list[str]:
    done = []
    for f in fields:
        if f["type"] != "file":
            continue
        label = (f["label"] + " " + f.get("name", "")).lower()
        target = cover_path if "cover" in label else resume if ("resume" in label or "cv" in label) else None
        if target is None and not done:
            target = resume  # first unnamed upload is almost always the resume
        if target:
            page.locator(f'[data-jobagent-key="{f["key"]}"]').set_input_files(str(target))
            done.append(f'{f["label"] or f["name"]} <- {target.name}')
    return done


def _find_submit(page):
    for sel in ["#btn-submit", "button[type=submit]", "input[type=submit]",
                "button:has-text('Submit application')", "button:has-text('Submit')"]:
        loc = page.locator(sel)
        if loc.count() and loc.first.is_visible():
            return loc.first
    return None


def _looks_submitted(page) -> bool:
    body = page.inner_text("body").lower()
    return any(s in body for s in ["thank you for applying", "application has been received",
                                   "application submitted", "thanks for applying", "we've received your application"])


def apply_to_job(llm: LLM, job: JobPosting, profile: CandidateProfile, resume_path: Path, cover_path: Path,
                 standard_answers: dict, auto_submit: bool = False, headless: bool = False,
                 screenshot_dir: Path | None = None) -> ApplyResult:
    if not job.can_auto_apply:
        return ApplyResult("manual", f"Apply manually at {job.apply_url or job.url}")

    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    cover_letter = cover_path.read_text(encoding="utf-8")
    url = job.apply_url or job.url
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            # Lever / some Greenhouse pages show the description first, with an "Apply" button.
            if not page.locator("input[type=file]").count():
                btn = page.locator("a:has-text('Apply for this job'), button:has-text('Apply'), a:has-text('Apply')")
                if btn.count():
                    btn.first.click()
                    page.wait_for_timeout(2500)

            fields = page.evaluate(EXTRACT_JS)
            if not fields:
                return ApplyResult("failed", "No form fields found on the page")
            uploaded = _upload_files(page, fields, resume_path, cover_path)
            page.wait_for_timeout(1500)  # some forms autofill from the resume; re-read after
            fields = page.evaluate(EXTRACT_JS)
            fillable = [f for f in fields if f["type"] != "file"]

            answers = _answer_form(llm, fillable, profile, job, cover_letter, standard_answers)
            by_key = {f["key"]: f for f in fillable}
            filled, failed = [], []
            for a in answers.answers:
                f = by_key.get(a.key)
                if f and a.value:
                    (filled if _fill(page, f, a.value) else failed).append(f["label"])

            missing_required = [by_key[k]["label"] for k in answers.needs_human
                                if k in by_key and by_key[k].get("required")]
            missing_required += [f"(fill failed) {label}" for label in failed]
            notes = (f"uploaded: {uploaded}; filled {len(filled)} fields; "
                     f"needs you: {[by_key[k]['label'] for k in answers.needs_human if k in by_key]}")
            if screenshot_dir:
                page.screenshot(path=str(screenshot_dir / "form_filled.png"), full_page=True)

            if auto_submit and not missing_required:
                submit = _find_submit(page)
                if not submit:
                    return ApplyResult("filled", notes + "; submit button not found")
                submit.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except PWTimeout:
                    pass
                page.wait_for_timeout(2000)
                if screenshot_dir:
                    page.screenshot(path=str(screenshot_dir / "after_submit.png"), full_page=True)
                if _looks_submitted(page):
                    return ApplyResult("applied", notes)
                return ApplyResult("filled", notes + "; clicked submit but no confirmation seen "
                                   "(CAPTCHA or validation error?) - check the screenshot")

            if headless:
                return ApplyResult("filled", notes + "; headless review mode - not submitted")
            # Human-in-the-loop: you review in the open browser, fix anything, and click Submit yourself.
            print(f"\n=== {job.company} — {job.title}")
            print(f"    Filled {len(filled)} fields. Please complete: {missing_required or 'nothing required'}")
            ans = input("    Review the browser window. Type 'y' once YOU have submitted, 's' to skip: ").strip().lower()
            if ans == "y":
                return ApplyResult("applied", notes + "; submitted by user")
            return ApplyResult("filled", notes + "; skipped by user")
        except Exception as e:  # noqa: BLE001
            log.exception("apply failed for %s", job.key)
            return ApplyResult("failed", str(e)[:500])
        finally:
            browser.close()
