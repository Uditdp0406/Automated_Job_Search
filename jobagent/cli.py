"""Command-line interface:  python -m jobagent <command>"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import load_settings
from .llm import LLMError


def _print_profile(p) -> None:
    print(f"\n{p.contact.full_name} — {p.headline}")
    print(f"Seniority: {p.seniority}  |  Experience: {p.years_of_experience:g} yrs")
    print(f"Top skills: {', '.join(p.skills[:15])}")
    print(f"Target titles: {', '.join(p.target_titles)}")
    print("\nStrengths:\n  - " + "\n  - ".join(p.strengths))
    print("\nGaps:\n  - " + "\n  - ".join(p.gaps or ["(none noted)"]))
    print("\nResume improvements:\n  - " + "\n  - ".join(p.resume_improvements))


def _print_status(tracker) -> None:
    rows = tracker.all()
    if not rows:
        print("No jobs tracked yet.")
        return
    print(f"{'SCORE':>5}  {'STATUS':<9} {'COMPANY':<22} {'TITLE':<45} KEY")
    for r in rows:
        score = "" if r["score"] is None else str(r["score"])
        print(f"{score:>5}  {r['status']:<9} {(r['company'] or '')[:22]:<22} {(r['title'] or '')[:45]:<45} {r['key']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jobagent", description="AI job search agent powered by Claude")
    ap.add_argument("-c", "--config", default="config.yaml", help="config file (default: config.yaml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="analyze your resume")
    a.add_argument("--resume", required=True)
    sub.add_parser("search", help="find new jobs (and prefilter them)")
    s = sub.add_parser("score", help="have Claude score unscored jobs")
    s.add_argument("--limit", type=int)
    t = sub.add_parser("tailor", help="tailor resume + cover letter for one job")
    t.add_argument("key", help="job key from `status`")
    p = sub.add_parser("apply", help="tailor + fill/submit applications for top-scored jobs")
    p.add_argument("--limit", type=int)
    p.add_argument("--auto-submit", action="store_true", help="submit without waiting for your review")
    p.add_argument("--job", action="append", dest="keys", help="apply only to this job key (repeatable)")
    r = sub.add_parser("run", help="full pipeline: analyze -> search -> score -> apply")
    r.add_argument("--resume", help="resume file (omit to reuse the last analyzed one)")
    r.add_argument("--auto-submit", action="store_true")
    sub.add_parser("status", help="show tracked jobs")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "anthropic", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from .pipeline import Agent

    agent = Agent(load_settings(args.config))
    auto = True if getattr(args, "auto_submit", False) else None
    try:
        if args.cmd == "analyze":
            _print_profile(agent.analyze(args.resume))
        elif args.cmd == "search":
            print(f"{len(agent.search())} new jobs added. Next: jobagent score")
        elif args.cmd == "score":
            agent.score(args.limit)
            _print_status(agent.tracker)
        elif args.cmd == "tailor":
            resume, cover = agent.tailor(args.key)
            print(f"Resume: {resume}\nCover letter: {cover}")
        elif args.cmd == "apply":
            agent.apply(limit=args.limit, auto_submit=auto, keys=args.keys)
            _print_status(agent.tracker)
        elif args.cmd == "run":
            agent.run(args.resume, auto_submit=auto)
            _print_status(agent.tracker)
        elif args.cmd == "status":
            _print_status(agent.tracker)
    except LLMError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
