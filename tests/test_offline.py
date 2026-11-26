"""Offline tests - no Anthropic API calls, no network. Run: python -m pytest -q"""
from pathlib import Path

import pytest

from jobagent.config import SearchConfig
from jobagent.matcher import prefilter
from jobagent.models import (CandidateProfile, ContactInfo, EducationItem, ExperienceItem, FieldAnswer,
                             FitAssessment, FormAnswers, JobPosting, TailoredResume)
from jobagent.sources import GH_RE, LEVER_RE, html_to_text
from jobagent.tracker import Tracker

CONTACT = ContactInfo(full_name="Asha Rao", email="asha@example.com", phone="+91 90000 00000")


def profile() -> CandidateProfile:
    return CandidateProfile(
        contact=CONTACT, headline="Backend engineer", summary="Builds APIs", years_of_experience=4,
        seniority="mid", skills=["Python", "Django", "PostgreSQL"],
        experience=[ExperienceItem(company="Acme", title="Software Engineer", bullets=["Built APIs"])],
        education=[EducationItem(institution="VTU", degree="B.E.", field="CS")],
        target_titles=["Backend Engineer", "Python Developer"], search_keywords=["python"],
        strengths=["APIs"], gaps=[], resume_improvements=[],
    )


def job(**kw) -> JobPosting:
    base = dict(source="lever", source_id="acme/1", company="Acme", title="Senior Backend Engineer",
                location="Bengaluru, India", url="https://jobs.lever.co/acme/1", description="Python, Django")
    base.update(kw)
    return JobPosting(**base)


def test_prefilter():
    cfg = SearchConfig(locations=["Bengaluru"], exclude_keywords=["clearance"])
    assert prefilter(job(), profile(), cfg)[0]
    assert not prefilter(job(title="Head of Sales"), profile(), cfg)[0]
    assert not prefilter(job(location="Berlin"), profile(), cfg)[0]
    assert prefilter(job(location="Remote - Worldwide", remote=True), profile(), cfg)[0]
    assert not prefilter(job(description="needs security clearance"), profile(), cfg)[0]


def test_tracker_dedup_and_status(tmp_path):
    t = Tracker(tmp_path / "j.db")
    assert t.add(job()) and not t.add(job())
    fit = FitAssessment(score=88, recommendation="apply", matched_requirements=[], missing_requirements=[],
                        dealbreakers=[], reasoning="", keywords_to_emphasize=[])
    t.set_assessment("lever:acme/1", fit, "scored")
    assert [r["key"] for r in t.by_status("scored", min_score=80)] == ["lever:acme/1"]
    assert t.assessment("lever:acme/1").score == 88


def test_url_patterns_and_html():
    txt = "https://job-boards.greenhouse.io/stripe/jobs/123\nhttps://jobs.lever.co/netflix/abc"
    assert GH_RE.findall(txt) == ["stripe"] and LEVER_RE.findall(txt) == ["netflix"]
    assert html_to_text("&lt;p&gt;Hi&lt;/p&gt;&lt;ul&gt;&lt;li&gt;A&lt;/li&gt;&lt;/ul&gt;") == "Hi\nA"


def test_render_docx(tmp_path):
    from jobagent.tailor import render_docx
    p = profile()
    r = TailoredResume(contact=CONTACT, headline=p.headline, summary=p.summary, skills=p.skills,
                       experience=p.experience, education=p.education, change_log=[])
    out = render_docx(r, tmp_path / "r.docx")
    import docx
    text = "\n".join(x.text for x in docx.Document(str(out)).paragraphs)
    assert "Asha Rao" in text and "Built APIs" in text


FORM = """<html><body><form>
<label for="name">Full name *</label><input id="name" name="name" required>
<label for="email">Email *</label><input id="email" name="email" type="email" required>
<label for="resume">Resume/CV</label><input id="resume" name="resume" type="file">
<label for="exp">Country</label><select id="exp"><option></option><option>India</option><option>USA</option></select>
<fieldset><legend>Do you require visa sponsorship?</legend>
 <label><input type="radio" name="spons" value="y">Yes</label><label><input type="radio" name="spons" value="n">No</label></fieldset>
<label for="why">Why us?</label><textarea id="why"></textarea>
<button type="submit">Submit application</button></form></body></html>"""


class FakeLLM:
    def parse(self, *, schema, **_):
        assert schema is FormAnswers
        return FormAnswers(answers=[
            FieldAnswer(key="k0", value="Asha Rao", confidence="high"),
            FieldAnswer(key="k1", value="asha@example.com", confidence="high"),
            FieldAnswer(key="k3", value="India", confidence="high"),
            FieldAnswer(key="k4", value="No", confidence="high"),
            FieldAnswer(key="k5", value="I like APIs.", confidence="medium"),
        ], needs_human=[])


def test_form_fill_with_browser(tmp_path):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright
    from jobagent.applier import EXTRACT_JS, _answer_form, _fill, _upload_files

    resume = tmp_path / "r.pdf"; resume.write_bytes(b"%PDF-1.4")
    cover = tmp_path / "c.txt"; cover.write_text("hi")
    with sync_playwright() as pw:
        exe = "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None
        try:
            b = pw.chromium.launch(**({"executable_path": exe} if exe else {}))
        except Exception as e:  # browser not installed
            pytest.skip(str(e))
        page = b.new_page(); page.set_content(FORM)
        fields = page.evaluate(EXTRACT_JS)
        labels = {f["key"]: (f["type"], f["label"]) for f in fields}
        assert labels["k2"][0] == "file" and labels["k4"][0] == "radio"
        assert "sponsorship" in labels["k4"][1]
        assert _upload_files(page, fields, resume, cover)
        ans = _answer_form(FakeLLM(), [f for f in fields if f["type"] != "file"], profile(), job(), "hi", {})
        by = {f["key"]: f for f in fields}
        assert all(_fill(page, by[a.key], a.value) for a in ans.answers)
        assert page.input_value("#name") == "Asha Rao"
        assert page.eval_on_selector("select", "e => e.options[e.selectedIndex].text") == "India"
        assert page.is_checked("input[value=n]")
        b.close()


# ------------------------------------------------------------- regression tests for review fixes --
def test_greenhouse_apply_url_is_hosted_form(monkeypatch):
    from jobagent import sources
    monkeypatch.setattr(sources, "http_get_json", lambda *a, **k: {"jobs": [{
        "id": 42, "title": "Backend Engineer", "absolute_url": "https://stripe.com/jobs/listing/x?gh_jid=42",
        "location": {"name": "Remote"}, "content": "&lt;p&gt;Python&lt;/p&gt;", "updated_at": "2026-11-01T00:00:00Z"}]})
    [j] = sources.fetch_greenhouse("stripe")
    assert j.apply_url == "https://boards.greenhouse.io/embed/job_app?for=stripe&token=42"
    assert j.description == "Python" and j.remote and j.can_auto_apply


def test_age_filter():
    from jobagent.sources import age_days
    assert age_days("2000-01-01T00:00:00Z") > 1000
    assert age_days("") is None and age_days("garbage") is None
    assert age_days("1700000000000") > 0  # Lever epoch ms
    cfg = SearchConfig(max_age_days=30)
    assert not prefilter(job(posted_at="2020-01-01"), profile(), cfg)[0]
    assert prefilter(job(posted_at=""), profile(), cfg)[0]


def test_standard_answers_placeholders_ignored():
    from jobagent.config import ApplyConfig
    a = ApplyConfig(standard_answers={"visa": "TODO Yes / No", "notice": "30 days", "x": "  ", "y": "fill me"})
    assert a.real_answers() == {"notice": "30 days"}


def test_relevance_ranks_better_matches_first():
    from jobagent.matcher import relevance
    cfg = SearchConfig()
    good = job(title="Backend Engineer", description="Python Django PostgreSQL")
    meh = job(title="Engineer", description="Java")
    assert relevance(good, profile(), cfg) > relevance(meh, profile(), cfg)


def _browser(pw):
    exe = "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None
    try:
        return pw.chromium.launch(**({"executable_path": exe} if exe else {}))
    except Exception as e:  # noqa: BLE001
        pytest.skip(str(e))


def test_rescan_clears_stale_keys_and_detects_empty_required():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright
    from jobagent.applier import EMPTY_REQUIRED_JS, EXTRACT_JS
    with sync_playwright() as pw:
        b = _browser(pw); page = b.new_page(); page.set_content(FORM)
        page.evaluate(EXTRACT_JS)
        page.eval_on_selector("#name", "e => { e.previousElementSibling.remove(); e.style.display = 'none'; }")
        fields = page.evaluate(EXTRACT_JS)
        keys = [f["key"] for f in fields]
        for k in keys:  # every key must match exactly one element (or one radio group)
            assert page.locator(f'[data-jobagent-key="{k}"]').count() <= 1
        email = next(f for f in fields if "Email" in f["label"])
        assert page.evaluate(EMPTY_REQUIRED_JS, [email["key"]]) == [email["key"]]
        page.fill(f'[data-jobagent-key="{email["key"]}"]', "a@b.co")
        assert page.evaluate(EMPTY_REQUIRED_JS, [email["key"]]) == []
        b.close()


COMBO = """<html><body>
<label id="c-label" for="c">Country *</label>
<input id="c" role="combobox" aria-labelledby="c-label" required
  onfocus="document.getElementById('lb').style.display='block'">
<div id="lb" role="listbox" style="display:none">
  <div role="option" onclick="c.value='India'">India</div><div role="option" onclick="c.value='Japan'">Japan</div>
</div>
<a href="#" id="li">Apply with LinkedIn</a><button id="ap">Apply for this job</button>
</body></html>"""


def test_combobox_options_and_safe_apply_button():
    pytest.importorskip("playwright")
    import re
    from playwright.sync_api import sync_playwright
    from jobagent.applier import EXTRACT_JS, _collect_combobox_options, _fill
    with sync_playwright() as pw:
        b = _browser(pw); page = b.new_page(); page.set_content(COMBO)
        fields = page.evaluate(EXTRACT_JS)
        [combo] = [f for f in fields if f["type"] == "combobox"]
        _collect_combobox_options(page, fields)
        assert combo["options"] == ["India", "Japan"]
        assert _fill(page, combo, "India") and page.input_value("#c") == "India"
        btn = page.locator("a, button").filter(has_text=re.compile(r"^\s*(apply( for this job| now)?)\s*$", re.I))
        assert btn.count() == 1 and btn.first.get_attribute("id") == "ap"
        b.close()


def test_select_fuzzy_match():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright
    from jobagent.applier import EXTRACT_JS, _fill
    with sync_playwright() as pw:
        b = _browser(pw); page = b.new_page(); page.set_content(FORM)
        sel = next(f for f in page.evaluate(EXTRACT_JS) if f["type"] == "select")
        assert _fill(page, sel, "india")            # case-insensitive
        assert not _fill(page, sel, "Atlantis")     # unknown option -> reported as failed, not guessed
        b.close()


def test_pipeline_score_and_apply_guards(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from jobagent.config import Settings
    from jobagent.pipeline import Agent
    s = Settings(data_dir=str(tmp_path / "d"), output_dir=str(tmp_path / "o"))
    Path(s.data_dir).mkdir(); Path(s.output_dir).mkdir()
    agent = Agent(s)
    s.profile_path.write_text(profile().model_dump_json()); s.resume_text_path.write_text("resume")
    for i, title in enumerate(["Backend Engineer", "Python Developer", "Backend Developer"]):
        agent.tracker.add(job(source_id=f"acme/{i}", title=title))

    class FakeLLM:
        def parse(self, *, schema, user, **_):
            if "Python Developer" in user:
                raise RuntimeError("boom")
            score = 90 if "Backend Engineer" in user else 40
            return FitAssessment(score=score, recommendation="apply" if score > 70 else "skip",
                                 matched_requirements=[], missing_requirements=[], dealbreakers=[],
                                 reasoning="", keywords_to_emphasize=[])
    agent.llm = FakeLLM()
    assert agent.score() == 3
    st = {r["key"]: r["status"] for r in agent.tracker.all()}
    assert st == {"lever:acme/0": "scored", "lever:acme/1": "found", "lever:acme/2": "skipped"}

    agent.tracker.update("lever:acme/0", status="applied")
    called = []
    monkeypatch.setattr("jobagent.applier.apply_to_job", lambda *a, **k: called.append(a))
    agent.apply(keys=["lever:acme/0", "nope"])
    assert called == []  # never re-applies, unknown keys ignored
