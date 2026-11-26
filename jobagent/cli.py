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


def _edit_boards(agent, add: list[str], remove: list[str]) -> None:
    t = agent.tracker
    for spec in add + remove:
        if ":" not in spec or spec.split(":", 1)[0] not in ("greenhouse", "lever"):
            raise SystemExit(f"bad board {spec!r}: use greenhouse:<token> or lever:<slug>")
    for spec in add:
        src, slug = spec.split(":", 1)
        t.add_board(src, slug, "manual")
        t.board_update(src, slug.lower(), disabled=0)
    for spec in remove:
        src, slug = spec.split(":", 1)
        t.board_update(src, slug.lower(), disabled=1)
    for b in agent.s.search.greenhouse_boards:
        t.add_board("greenhouse", b, "config")
    for c in agent.s.search.lever_companies:
        t.add_board("lever", c, "config")
    rows = t.boards(include_disabled=True)
    if not rows:
        print("Watch list is empty. Add companies:  jobagent boards --add greenhouse:stripe --add lever:netflix")
        return
    print(f"{'SOURCE':<11} {'COMPANY':<24} {'ORIGIN':<11} {'LAST CHECKED':<26} NOTE")
    for r in rows:
        note = "disabled" if r["disabled"] else (r["last_error"] or ("" if r["baselined_at"] else "not checked yet"))
        print(f"{r['source']:<11} {r['slug']:<24} {r['origin'] or '':<11} {r['last_polled_at'] or '-':<26} {note}")


def _pending(agent, ask=input, open_url=None) -> None:
    """Go through everything waiting for you: half-filled forms, docs ready for review, manual-apply sites."""
    import webbrowser
    open_url = open_url or webbrowser.open
    t = agent.tracker
    rows = t.by_status("filled", "tailored", "manual")
    if not rows:
        print("Nothing is waiting for you.")
        return
    print(f"{len(rows)} application(s) need you:\n")
    agent.s.apply.headless = False  # you're at the laptop: show the browser
    for i, r in enumerate(rows, 1):
        job = t.posting(r["key"])
        print(f"[{i}/{len(rows)}] {r['score'] or '?'}/100  {job.company} — {job.title}  ({r['status']})")
        if r["notes"]:
            print(f"    note: {r['notes'][-200:]}")
        if job.can_auto_apply:
            choice = ask("    Enter = open it in the browser (pre-filled), s = skip, q = quit: ").strip().lower()
            if choice == "q":
                break
            if choice != "s":
                agent.apply(keys=[r["key"]], auto_submit=False)
            continue
        print(f"    apply at : {job.apply_url or job.url}")
        print(f"    resume   : {r['resume_path'] or '-'}\n    cover    : {r['cover_letter_path'] or '-'}")
        open_url(job.apply_url or job.url)
        choice = ask("    Type y once you've applied, s = skip, q = quit: ").strip().lower()
        if choice == "q":
            break
        if choice == "y":
            t.update(r["key"], status="applied", notes="applied manually")
    print()
    _print_status(t)


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
    w = sub.add_parser("watch", help="keep checking for newly opened jobs; alert + apply immediately")
    w.add_argument("--once", action="store_true", help="do one check and exit")
    w.add_argument("--interval", type=float, help="minutes between checks (overrides config)")
    w.add_argument("--review", action="store_true", help="don't submit; prepare docs and alert you instead")
    b = sub.add_parser("boards", help="show / edit the companies being watched")
    b.add_argument("--add", action="append", default=[], metavar="SOURCE:SLUG",
                   help="e.g. greenhouse:stripe or lever:netflix (repeatable)")
    b.add_argument("--remove", action="append", default=[], metavar="SOURCE:SLUG")
    sub.add_parser("notify-test", help="send a test alert to every configured channel")
    sub.add_parser("pending", help="finish applications that need you (opens each one for review)")

    from .laptop import fix_console_encoding, load_dotenv
    fix_console_encoding()
    load_dotenv(".env")  # ANTHROPIC_API_KEY etc. without exporting them in every terminal
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
        elif args.cmd == "watch":
            from .watcher import AlreadyRunning, Watcher
            if args.interval:
                agent.s.watch.interval_minutes = args.interval
            if args.review:
                agent.s.watch.auto_submit = False
            try:
                Watcher(agent).run(once=args.once)
            except AlreadyRunning as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
        elif args.cmd == "boards":
            _edit_boards(agent, args.add, args.remove)
        elif args.cmd == "pending":
            _pending(agent)
        elif args.cmd == "notify-test":
            from .notify import Notifier
            n = Notifier(agent.s.notify)
            res = n.send("🔔 jobagent test alert", "If you can read this, job alerts will reach you here.",
                         url="https://github.com", urgent=False)
            for ch, ok in res.items():
                print(f"  {ch:<9} {'ok' if ok else 'FAILED (see warning above)'}")
            if not res:
                print("No channels enabled - see the notify: section of config.example.yaml")
    except LLMError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
