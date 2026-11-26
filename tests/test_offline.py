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
