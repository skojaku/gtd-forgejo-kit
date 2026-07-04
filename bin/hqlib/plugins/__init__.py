"""Plugin discovery: each subfolder here is a plugin, found by folder name.

Contract (see plugins/mail, plugins/cal, plugins/drive for full examples):
  - NAME: str, matches the folder name, becomes the `hq <NAME> ...` subcommand.
  - register(subparsers): required. Wires the `hq <NAME> ...` subcommand tree.
  - scan(cfg, state, qdir, taken) -> list[str]: optional. Called by
    `hq queue scan` for plugins that detect their own new-work events (new
    mail, etc.) — return the list of job filenames created.
  - handle_job(job_type, cfg, payload, log, job_name) -> (ok, err) | None:
    optional. Called by `hq queue work` for job types this plugin created via
    its own scan(). Return None to decline a job type (falls through to the
    generic prompt-driven LLM runner) rather than raising.

Plugins may import from hqlib.common / hqlib.forgejo / hqlib.queue (core) —
never from another plugin. That's what localizes a broken plugin's blast
radius to itself instead of taking down the others.
"""
import importlib
import pkgutil


def discover():
    """All plugin modules, sorted by NAME. Import errors in one plugin don't
    take down the others or the CLI itself — reported, not raised."""
    import sys
    mods = []
    for _, name, ispkg in pkgutil.iter_modules(__path__):
        if not ispkg:
            continue
        try:
            mod = importlib.import_module(f".{name}", package=__name__)
        except Exception as e:  # noqa: BLE001 - a broken plugin must not break the CLI
            print(f"warning: plugin '{name}' failed to load: {e}", file=sys.stderr)
            continue
        if getattr(mod, "NAME", None) != name:
            print(f"warning: plugin folder '{name}' has NAME={mod.NAME!r} — must match "
                  f"the folder name, skipping", file=sys.stderr)
            continue
        mods.append(mod)
    return sorted(mods, key=lambda m: m.NAME)
