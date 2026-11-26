"""Entry point for scheduled runs (cron / launchd / Task Scheduler).

Works whatever the working directory is: it puts the project root on sys.path itself, so the
scheduled job doesn't depend on the folder it starts in or on PYTHONPATH."""
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
if sys.path and os.path.abspath(sys.path[0]) == here:
    sys.path.pop(0)  # don't let jobagent/config.py etc. shadow other top-level modules
sys.path.insert(0, os.path.dirname(here))

from jobagent.cli import main  # noqa: E402

raise SystemExit(main())
