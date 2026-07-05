"""hq cron — scheduled housekeeping sweeps (the automation entry points).

`daily`   — un-defer tasks whose defer date has arrived and promote tasks
            scheduled for today into Next.
`cleanup` — reconcile issue open/closed state with the status/done label.

These run from the hq-cron container's crontab (see docker/crontab). The
sweep logic itself lives in task.py (it operates on GTD tasks); this module
only wires it under a single `hq cron` domain so every recurring entry point
is discoverable in one place.
"""
from . import task


def register(sub):
    p = sub.add_parser("cron", help="scheduled housekeeping sweeps (run by the hq-cron container)")
    s2 = p.add_subparsers(dest="cron_cmd", required=True)

    s = s2.add_parser("daily", help="daily un-defer / schedule sweep")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=task.cmd_cron_daily)

    s = s2.add_parser("cleanup", help="close status/done issues; label closed issues done")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=task.cmd_cron_cleanup)
