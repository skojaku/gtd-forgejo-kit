"""hq install — dependency check + config scaffolding for a plugin.

Mechanical only, not the guided walkthrough (that's .agents/skills/setup):
- verifies the binaries a plugin needs are on PATH, with an install hint
  for each one missing
- creates config/hq.yaml from config/hq.example.yaml if it doesn't exist
  yet, then deep-merges in the plugin's CONFIG_STUB — only keys entirely
  absent are added, so a value the user already set is never touched

Plugin contract additions (both optional, see hqlib/plugins/__init__.py):
  BIN_DEPS: list[str]      — executables this plugin's commands shell out to
  CONFIG_STUB: dict        — hq.yaml section(s) to scaffold with placeholders
"""
import shutil

from .common import repo_root, config_path, print_json, fail
from . import plugins as plugins_pkg

# ruamel is only needed by `hq install` (comment-preserving config scaffolding),
# which runs at setup time on an interactive host — never in the cron container.
# Import it lazily so the core CLI (task/queue/triage/doctor) stays stdlib+PyYAML
# only and does not hard-fail where ruamel isn't installed.
_yaml = None


def _get_yaml():
    global _yaml
    if _yaml is None:
        try:
            from ruamel.yaml import YAML
        except ModuleNotFoundError:
            fail("`hq install` needs the ruamel.yaml package: pip install ruamel.yaml")
        _yaml = YAML()
        _yaml.preserve_quotes = True
        _yaml.indent(mapping=2, sequence=2, offset=0)
    return _yaml

CORE_BIN_DEPS = ["rg"]

INSTALL_HINTS = {
    "rg": "brew install ripgrep / apt install ripgrep",
    "gws": "https://github.com/google/googleworkspace-cli (pip install / npm install per its README)",
}


def _missing_bins(names):
    return [n for n in names if shutil.which(n) is None]


def _ensure_config():
    """Copy hq.example.yaml -> hq.yaml if the latter doesn't exist yet.
    Returns True if it just created the file."""
    path = config_path()
    if path.exists():
        return False
    example = repo_root() / "config" / "hq.example.yaml"
    if not example.exists():
        return False
    path.write_text(example.read_text())
    return True


def _merge_stub(stub):
    """Deep-merge `stub` into hq.yaml, adding only keys that are entirely
    absent. Returns the dotted paths of keys it added (empty if none).
    Uses ruamel's round-trip loader so existing comments/formatting in a
    user's live hq.yaml survive the write — plain PyYAML would flatten them."""
    path = config_path()
    current = _get_yaml().load(path.read_text()) if path.exists() else None
    if current is None:
        current = {}

    added = []

    def merge(dst, src, prefix):
        for k, v in src.items():
            dotted = f"{prefix}{k}"
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v, f"{dotted}.")
            elif k not in dst:
                dst[k] = v
                added.append(dotted)

    merge(current, stub, "")
    if added:
        with path.open("w") as f:
            _get_yaml().dump(current, f)
    return added


def _install_one(name):
    mod = None
    if name != "core":
        mods = {m.NAME: m for m in plugins_pkg.discover()}
        mod = mods.get(name)
        if not mod:
            fail(f"unknown plugin '{name}'. Known: {sorted(mods)} (or 'core')")

    result = {"plugin": name, "created_config": False,
              "missing_bin_deps": [], "config_keys_added": [], "notes": []}

    result["created_config"] = _ensure_config()

    bin_deps = list(CORE_BIN_DEPS) if mod is None else getattr(mod, "BIN_DEPS", [])
    result["missing_bin_deps"] = [
        {"bin": b, "hint": INSTALL_HINTS.get(b, "check the tool's own docs")}
        for b in _missing_bins(bin_deps)
    ]

    stub = getattr(mod, "CONFIG_STUB", None) if mod is not None else None
    if stub:
        result["config_keys_added"] = _merge_stub(stub)

    if result["missing_bin_deps"]:
        result["notes"].append(f"install the missing binaries above, then re-run `hq install {name}`")
    if result["config_keys_added"]:
        result["notes"].append(f"edit config/hq.yaml to fill in the new '{name}' keys (placeholders are empty)")
    if not any([result["missing_bin_deps"], result["config_keys_added"], result["created_config"]]):
        result["notes"].append("already fully configured")
    return result


def cmd_install(args):
    if args.name == "all":
        names = ["core"] + sorted(m.NAME for m in plugins_pkg.discover())
        print_json([_install_one(n) for n in names])
    else:
        print_json(_install_one(args.name))


def register(sub):
    p = sub.add_parser(
        "install",
        help="check deps + scaffold config for a plugin (mechanical; see setup skill for the guided walkthrough)",
    )
    p.add_argument("name", help="plugin name (mail/cal/drive/...), 'core', or 'all'")
    p.set_defaults(func=cmd_install)
