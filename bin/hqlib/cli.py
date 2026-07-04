"""Argparse tree for the hq CLI.

Core domains (task, wiki, queue) are always wired in directly — they're
source-agnostic and don't depend on any plugin. Everything else (mail, cal,
drive, ...) is a plugin: a folder under hqlib/plugins/ discovered by name,
each contributing its own `hq <name> ...` subcommand. See
hqlib/plugins/__init__.py for the plugin contract.
"""
import argparse

from . import task, wiki, queue, plugins


def build_parser():
    p = argparse.ArgumentParser(
        prog="hq",
        description=(
            "Unified HQ CLI. Core: task (GTD project), wiki (search), queue (automation). "
            "Plugins (discovered from hqlib/plugins/): mail (Gmail, drafts only), "
            "cal (calendar, policy-aware), drive (read-only). "
            "All output is small JSON; errors are {\"error\": ...} with exit 1."
        ),
    )
    sub = p.add_subparsers(dest="domain", required=True)
    task.register(sub)
    wiki.register(sub)
    queue.register(sub)
    for plugin in plugins.discover():
        plugin.register(sub)
    return p


def main():
    args = build_parser().parse_args()
    args.func(args)
