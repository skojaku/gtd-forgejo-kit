"""Argparse tree for the hq CLI."""
import argparse

from . import task, mail, cal, drive, wiki, queue


def build_parser():
    p = argparse.ArgumentParser(
        prog="hq",
        description=(
            "Unified HQ CLI. Domains: task (GTD project), mail (Gmail, drafts only), "
            "cal (calendar, policy-aware), drive (read-only), wiki (search), queue (automation). "
            "All output is small JSON; errors are {\"error\": ...} with exit 1."
        ),
    )
    sub = p.add_subparsers(dest="domain", required=True)
    task.register(sub)
    mail.register(sub)
    cal.register(sub)
    drive.register(sub)
    wiki.register(sub)
    queue.register(sub)
    return p


def main():
    args = build_parser().parse_args()
    args.func(args)
