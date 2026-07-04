"""hq queue — event detection and the job queue between the master and workers.

Roles (config: queue.* in env.yaml):
- master (always-on entry point): `hq queue scan` (cron) detects new issue
  activity plus whatever event sources each plugin's `scan()` hook adds
  (e.g. the mail plugin's new-unread-mail detection), and writes job files;
  small-model triage jobs are processed locally with `hq queue work --types
  triage`.
- workers (heavier local models): `hq queue work --types collect --remote
  <master-ssh-alias>` claims jobs over ssh and runs them against their own
  local model.

Jobs are JSON files under queue/pending|processing|done|failed. Retries are
encoded in the filename (….r0.json → ….r1.json) so remote claiming never has
to rewrite file contents. Everything here is deterministic — the LLM only
runs inside `work`, via pi, with a per-type prompt template.

Job types: "collect" is handled here directly (run_collect_job, core — it's
Forgejo-native, not source-specific). Any other type is offered to each
plugin's optional `handle_job()` hook first (e.g. the mail plugin owns
"thread_update"); if no plugin claims it, it falls through to the generic
prompt-driven `run_job_llm` (this is how "triage" is processed — no
plugin-specific code needed for a job type that's just "run this prompt").
"""
import json
import os
import re
import shlex
import subprocess
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from .common import fail, load_config, owner, repo_name, forgejo_url, repo_root, print_json
from .forgejo import ForgejoClient

MAX_RETRIES = 3
SEEN_IDS_CAP = 500
SSH_OPTS = ["-o", "ClearAllForwardings=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def queue_cfg(cfg):
    return cfg.get("queue") or {}


def queue_dir(cfg):
    d = repo_root() / queue_cfg(cfg).get("dir", "queue")
    for sub in ("pending", "processing", "done", "failed"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def now_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def job_filename(job_type, target, retries=0):
    return f"{now_stamp()}-{job_type}-{target}.r{retries}.json"


def parse_job_filename(name):
    """→ (type, target, retries) or None."""
    if not name.endswith(".json"):
        return None
    stem = name[: -len(".json")]
    if ".r" not in stem:
        return None
    head, _, r = stem.rpartition(".r")
    try:
        retries = int(r)
    except ValueError:
        return None
    parts = head.split("-", 2)
    if len(parts) < 3:
        return None
    return parts[1], parts[2], retries


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def state_path(cfg):
    return queue_dir(cfg) / "state.json"


def load_state(cfg):
    p = state_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {"seen_message_ids": [], "last_issue_scan": None, "last_full_sweep": None}


def save_state(cfg, state):
    state["seen_message_ids"] = state.get("seen_message_ids", [])[-SEEN_IDS_CAP:]
    state_path(cfg).write_text(json.dumps(state, indent=2))


def existing_targets(qdir):
    """(type, target) pairs already pending or processing — the dedup set."""
    pairs = set()
    for sub in ("pending", "processing"):
        for f in (qdir / sub).glob("*.json"):
            parsed = parse_job_filename(f.name)
            if parsed:
                pairs.add((parsed[0], parsed[1]))
    return pairs


def enqueue(qdir, job_type, target, payload):
    path = qdir / "pending" / job_filename(job_type, target)
    payload = dict(payload, type=job_type, created=now_stamp())
    path.write_text(json.dumps(payload, indent=2))
    return path.name


# --------------------------------------------------------------------------
# Thread tracking — link a producer's own reference (e.g. a Gmail thread id,
# via the mail plugin's `<!-- email-thread:... -->` marker) to the issue its
# triage created, so a later event on that thread logs onto the issue and
# refreshes its dossier instead of spawning a duplicate card. The marker
# format itself is a plugin convention (currently only the mail plugin
# writes one); core just knows the one regex, not any plugin's code.
# --------------------------------------------------------------------------

_EMAIL_THREAD_RE = re.compile(r"<!--\s*email-thread:([A-Za-z0-9_-]+)\s*-->")


def _client(cfg):
    return ForgejoClient(forgejo_url(cfg), owner(cfg), repo_name(cfg))


# --------------------------------------------------------------------------
# scan — deterministic event detection (runs on the master)
# --------------------------------------------------------------------------

def scan_issues(cfg, state, qdir, taken):
    from .dossier import MARKER_PREFIX
    client = _client(cfg)
    user_login = owner(cfg)
    since = state.get("last_issue_scan")
    scan_started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    issues = client.list_issues(state="open", since=since)

    created = []
    for issue in issues:
        n = issue["number"]
        # Learn a producer's thread ↔ issue link from the marker embedded in
        # the body (see module docstring above), so that producer's own scan
        # can route future events here. Free — this scan already fetched the
        # body. Drop the mapping if the issue has closed.
        mt = _EMAIL_THREAD_RE.search(issue.get("body") or "")
        if mt:
            tmap = state.setdefault("email_threads", {})
            if (issue.get("state") or "").lower() == "closed":
                tmap.pop(mt.group(1), None)
            else:
                tmap[mt.group(1)] = n
        if ("collect", str(n)) in taken:
            continue
        needs_collect = False
        if since is None or issue.get("created_at", "") > since:
            needs_collect = True
        else:
            comments = client.list_comments(n)
            for c in comments:
                body = c.get("body") or ""
                if body.startswith(MARKER_PREFIX):
                    continue
                if c.get("user", {}).get("login") == user_login:
                    needs_collect = True
                    break
        if needs_collect:
            created.append(enqueue(qdir, "collect", str(n), {"issue": n}))
            taken.add(("collect", str(n)))

    state["last_issue_scan"] = scan_started
    return created


def cmd_scan(args):
    cfg = load_config()
    qdir = queue_dir(cfg)
    state = load_state(cfg)
    taken = existing_targets(qdir)

    from . import plugins

    created = []
    created += scan_issues(cfg, state, qdir, taken)
    for plugin in plugins.discover():
        plugin_scan = getattr(plugin, "scan", None)
        if plugin_scan:
            created += plugin_scan(cfg, state, qdir, taken) or []

    # Daily backstop: refresh dossiers on active tasks. Expanded here into
    # per-issue collect jobs — one issue per LLM invocation, no batch job.
    today = date.today().isoformat()
    if state.get("last_full_sweep") != today:
        from .task import fetch_tasks
        for t in fetch_tasks(cfg):
            if (t["status"] or "").lower() in ("next", "waiting") and t["issue"]:
                target = str(t["issue"])
                if ("collect", target) not in taken:
                    created.append(enqueue(qdir, "collect", target, {"issue": t["issue"]}))
                    taken.add(("collect", target))
        state["last_full_sweep"] = today

    save_state(cfg, state)
    print_json({"created": created, "pending": len(list((qdir / "pending").glob("*.json")))})


# --------------------------------------------------------------------------
# work — process jobs with pi (local queue or remote over ssh)
# --------------------------------------------------------------------------

def runner_cfg(cfg, job_type):
    runners = queue_cfg(cfg).get("runners") or {}
    rc = runners.get(job_type) or {}
    prompt = rc.get("prompt", f"scripts/prompts/{job_type}.md")
    pi_args = rc.get("pi_args", [])
    # Per-host pi_args override so aster's fallback collect can pin a local model
    # (e.g. --provider ollama --model qwen3.6:27b) WITHOUT editing the shared
    # config the Mac also reads. Env name: HQ_<TYPE>_PI_ARGS (e.g.
    # HQ_COLLECT_PI_ARGS). Empty/unset -> config default.
    override = os.environ.get(f"HQ_{job_type.upper().replace('-', '_')}_PI_ARGS")
    if override:
        pi_args = shlex.split(override)
    return {
        "pi_args": pi_args,
        "prompt": prompt,
        "warm_url": rc.get("warm_url"),
        "timeout_min": rc.get("timeout_min", 20),
    }


def warm_endpoint(url, wait_s=90):
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5):
                return True
        except OSError:
            time.sleep(5)
    return False


def render_prompt(template_path, payload):
    try:
        text = (repo_root() / template_path).read_text()
    except OSError as e:
        fail(f"could not read prompt template {template_path}: {e}")
    for key, value in payload.items():
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        text = text.replace("{{" + key + "}}", str(value))
    return text


def run_job_llm(cfg, job_type, payload, log, job_name="job"):
    rc = runner_cfg(cfg, job_type)
    if rc["warm_url"] and not warm_endpoint(rc["warm_url"]):
        return False, f"endpoint {rc['warm_url']} did not come up"
    prompt = render_prompt(rc["prompt"], payload)
    cmd = ["pi"] + list(rc["pi_args"]) + ["--no-session", "-p", prompt]

    def write_log(stdout, stderr, note=""):
        logs_dir = queue_dir(cfg) / "logs"
        logs_dir.mkdir(exist_ok=True)
        (logs_dir / f"{job_name}.log").write_text(
            f"$ {' '.join(shlex.quote(c) for c in cmd)}\n{note}\n"
            f"--- stdout ---\n{stdout or ''}\n--- stderr ---\n{stderr or ''}"
        )

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=rc["timeout_min"] * 60, cwd=str(repo_root()),
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        write_log(out, err, note=f"TIMED OUT after {rc['timeout_min']} min")
        return False, f"pi timed out after {rc['timeout_min']} min"
    except FileNotFoundError:
        return False, "pi is not installed on this host"
    write_log(proc.stdout, proc.stderr, note=f"exit {proc.returncode}")
    log.append({"cmd": " ".join(shlex.quote(c) for c in cmd[:5]) + " …",
                "exit": proc.returncode,
                "tail": (proc.stdout + proc.stderr)[-500:]})
    return proc.returncode == 0, None if proc.returncode == 0 else f"pi exited {proc.returncode}"


class LocalQueue:
    def __init__(self, cfg):
        self.qdir = queue_dir(cfg)

    def list_pending(self):
        return sorted(f.name for f in (self.qdir / "pending").glob("*.json"))

    def claim(self, name):
        src = self.qdir / "pending" / name
        dst = self.qdir / "processing" / name
        src.rename(dst)
        return json.loads(dst.read_text())

    def finish(self, name, ok):
        src = self.qdir / "processing" / name
        if ok:
            src.rename(self.qdir / "done" / name)
            return "done"
        parsed = parse_job_filename(name)
        retries = parsed[2] + 1
        new_name = name.rsplit(".r", 1)[0] + f".r{retries}.json"
        if retries > MAX_RETRIES:
            src.rename(self.qdir / "failed" / new_name)
            return "failed"
        src.rename(self.qdir / "pending" / new_name)
        return "requeued"


class RemoteQueue:
    """Same operations over ssh; the queue lives on the remote host."""

    def __init__(self, cfg, host):
        self.host = host
        self.path = queue_cfg(cfg).get("remote_path", "~/HQ") + "/" + queue_cfg(cfg).get("dir", "queue")

    def _ssh(self, command, check=True):
        proc = subprocess.run(["ssh"] + SSH_OPTS + [self.host, command],
                              capture_output=True, text=True)
        if check and proc.returncode != 0:
            fail(f"ssh {self.host} `{command}` failed: {proc.stderr.strip()[:300]}")
        return proc

    def list_pending(self):
        proc = self._ssh(f"ls {self.path}/pending 2>/dev/null", check=False)
        return sorted(n for n in proc.stdout.split() if n.endswith(".json"))

    def claim(self, name):
        proc = self._ssh(
            f"mv {self.path}/pending/{shlex.quote(name)} {self.path}/processing/ "
            f"&& cat {self.path}/processing/{shlex.quote(name)}"
        )
        return json.loads(proc.stdout)

    def finish(self, name, ok):
        if ok:
            self._ssh(f"mv {self.path}/processing/{shlex.quote(name)} {self.path}/done/")
            return "done"
        parsed = parse_job_filename(name)
        retries = parsed[2] + 1
        new_name = name.rsplit(".r", 1)[0] + f".r{retries}.json"
        dest = "failed" if retries > MAX_RETRIES else "pending"
        self._ssh(f"mv {self.path}/processing/{shlex.quote(name)} {self.path}/{dest}/{shlex.quote(new_name)}")
        return "failed" if dest == "failed" else "requeued"


def run_collect_job(cfg, issue, log, job_name, force=False):
    """Collect with deterministic bookends on the runner side: freshness is
    checked in code, and the dossier is posted by US from the file the model
    writes — models (even 27B) reliably produce the JSON but skip the final
    posting call, so it is no longer their job. ``force`` bypasses the freshness
    gate (used when an email thread evolved and the dossier must be regenerated
    even though no new user comment arrived)."""
    from .dossier import brief_data, is_fresh, post_dossier

    brief = brief_data(cfg, issue)
    if brief.get("state") == "closed":
        log.append({"job": job_name, "note": "issue closed — skipped"})
        return True, None
    if is_fresh(brief) and not force:
        log.append({"job": job_name, "note": "dossier fresh, no new user comments — skipped"})
        return True, None

    out_file = queue_dir(cfg) / "out" / f"dossier-{issue}.json"
    out_file.parent.mkdir(exist_ok=True)
    out_file.unlink(missing_ok=True)

    ok, err = run_job_llm(cfg, "collect", {"issue": issue, "out_file": str(out_file)},
                          log, job_name=job_name)
    if not ok:
        return False, err
    if not out_file.exists():
        return False, "model produced no dossier file"
    try:
        params = json.loads(out_file.read_text())
    except json.JSONDecodeError as e:
        return False, f"dossier file is not valid JSON: {e}"
    try:
        result = post_dossier(cfg, issue, params)
    except SystemExit:
        return False, "dossier file failed validation (see queue/out)"
    log.append({"job": job_name, "posted": result})
    out_file.unlink(missing_ok=True)
    return True, None


def _dispatch_to_plugin(job_type, cfg, payload, log, job_name):
    """Ask every plugin's handle_job() in turn whether it owns this job_type.
    Returns (ok, err) from the first plugin that claims it, or None if none
    do (falls through to the generic prompt-driven LLM runner)."""
    from . import plugins
    for plugin in plugins.discover():
        handler = getattr(plugin, "handle_job", None)
        if not handler:
            continue
        result = handler(job_type, cfg, payload, log, job_name)
        if result is not None:
            return result
    return None


def cmd_work(args):
    cfg = load_config()
    types = [t.strip() for t in args.types.split(",")] if args.types else None
    q = RemoteQueue(cfg, args.remote) if args.remote else LocalQueue(cfg)

    processed = []
    log = []
    for name in q.list_pending():
        if len(processed) >= args.max:
            break
        parsed = parse_job_filename(name)
        if not parsed:
            continue
        job_type, target, _ = parsed
        if types and job_type not in types:
            continue
        payload = q.claim(name)
        if job_type == "collect":
            ok, err = run_collect_job(cfg, payload.get("issue") or int(target), log,
                                      job_name=name, force=payload.get("force", False))
        else:
            result = _dispatch_to_plugin(job_type, cfg, payload, log, name)
            if result is not None:
                ok, err = result
            else:
                ok, err = run_job_llm(cfg, job_type, payload, log, job_name=name)
        outcome = q.finish(name, ok)
        processed.append({"job": name, "type": job_type, "target": target,
                          "outcome": outcome, **({"error": err} if err else {})})

    print_json({"processed": processed, "log": log if args.verbose else []})


def cmd_list(args):
    cfg = load_config()
    qdir = queue_dir(cfg)
    out = {}
    for sub in ("pending", "processing", "done", "failed"):
        names = sorted(f.name for f in (qdir / sub).glob("*.json"))
        out[sub] = {"count": len(names), "jobs": names[-10:]}
    out["state"] = load_state(cfg)
    out["state"]["seen_message_ids"] = len(out["state"].get("seen_message_ids", []))
    print_json(out)


def cmd_add(args):
    cfg = load_config()
    qdir = queue_dir(cfg)
    payload = {}
    target = args.target or args.type
    if args.issue:
        payload["issue"] = args.issue
        target = str(args.issue)
    name = enqueue(qdir, args.type, target, payload)
    print_json({"created": name})


def register(sub):
    p = sub.add_parser("queue", help="event detection + job queue (automation plumbing)")
    s2 = p.add_subparsers(dest="queue_cmd", required=True)

    s = s2.add_parser("scan", help="detect new mail/issue activity and enqueue jobs (runs on the entry-point host)")
    s.set_defaults(func=cmd_scan)

    s = s2.add_parser("work", help="process pending jobs with pi (per-type model config in queue.runners)")
    s.add_argument("--types", help="comma-separated job types to handle (e.g. triage or collect,full-sweep)")
    s.add_argument("--remote", help="claim jobs from this ssh host's queue instead of the local one")
    s.add_argument("--max", type=int, default=5)
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_work)

    s2.add_parser("list", help="queue contents and scan state").set_defaults(func=cmd_list)

    s = s2.add_parser("add", help="manually enqueue a job (testing)")
    s.add_argument("type", choices=["triage", "collect"])
    s.add_argument("--issue", type=int)
    s.add_argument("--target")
    s.set_defaults(func=cmd_add)
