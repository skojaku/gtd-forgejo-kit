"""hq queue — event detection and the job queue, backed by Forgejo issue labels.

Queue state lives on Forgejo, not on disk. A job is one of:

  * a **collect** job — the target GTD issue itself carries the lifecycle
    labels (collect enriches that issue in place, so no separate ticket is
    needed and the repo does not fill up with per-task job issues); or
  * a **triage / thread_update / forced-collect** job — a standalone `hq-job`
    issue whose body holds the JSON payload (these are email events that have
    no task issue yet, or a collect that must carry a `force` flag).

Lifecycle is an exclusive scoped-label swap on the carrier issue:

    queue/pending  ->  queue/claimed:<worker>  ->  done

"done" means: close the `hq-job` ticket, or (for an in-place collect) drop the
lifecycle label from the still-open task issue. Claiming is atomic: adding
`queue/claimed:<worker>` auto-removes `queue/pending` (they share the exclusive
scope `queue`), then we re-read the issue and keep the job only if our own
claim label is the one that stuck — so when two workers race for the same job,
exactly one proceeds and the other skips.

Any worker with a Forgejo token can participate: no ssh, no shared filesystem,
no local queue-state file, survives container rebuilds. Only the per-worker
transcripts and the small scan cursor (seen mail ids / last-scan timestamps)
stay local, under queue/. The mail-thread ↔ issue marker convention lives
entirely in the mail plugin now (it owns that regex); core no longer knows it.

Two job classes, two runner mechanisms — matched to model capability
(PLAN.md workstream 4):

  * "triage" (small 4B model) is NOT agentic — it is one deterministic
    transformation. `run_triage_job` fetches the email itself, makes ONE direct
    ollama structured-output call (see hqlib/ollama.py), and applies the result
    via the existing `hq task add` / `hq mail draft-reply` code paths. No
    hermes, no soul file, no memory, no tool schemas.
  * "collect" (large 27B model) genuinely needs an agent loop across
    mail/drive/wiki, so `run_collect_job` drives `hermes -z -p hq-local` with
    the collect prompt (model from hq.yaml queue.runners.collect.model),
    pointed at ollama by the hq-local profile config.

Any other type is offered to each plugin's optional `handle_job()` hook first
(e.g. the mail plugin owns "thread_update"); if no plugin claims it, it falls
through to run_job_llm, which is now just a "no runner for this type" stub.
The LLM only ever runs inside `work`.
"""
import json
import os
import re
import shlex
import socket
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import ollama
from .common import fail, load_config, owner, client, repo_root, print_json

MAX_RETRIES = 3
SEEN_IDS_CAP = 2000

# Lifecycle labels. queue/pending, queue/failed and every queue/claimed:<w>
# share the exclusive scope "queue" (the text before the last "/"), so adding
# one Forgejo auto-removes the others — that is the atomic claim/complete swap.
JOB_LABEL = "hq-job"          # marks a standalone job ticket (not a GTD task)
L_PENDING = "queue/pending"
L_FAILED = "queue/failed"
CLAIM_PREFIX = "queue/claimed:"

_JOB_RE = re.compile(r"<!--\s*hq-job (\{.*\})\s*-->", re.DOTALL)


def queue_cfg(cfg):
    return cfg.get("queue") or {}


def queue_dir(cfg):
    """Local scratch only: transcripts (logs/) and the collect dossier hand-off
    (out/). Job STATE lives on Forgejo, not here."""
    d = repo_root() / queue_cfg(cfg).get("dir", "queue")
    for sub in ("logs", "out"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def now_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def worker_name():
    """Identity for the claimed:<worker> label. $HQ_WORKER_NAME wins; else the
    short hostname."""
    name = os.environ.get("HQ_WORKER_NAME")
    if name and name.strip():
        return name.strip()
    return socket.gethostname().split(".")[0] or "worker"


def _parse_rfc3339(text):
    """Forgejo timestamp → epoch seconds, or None."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Scan cursor — a tiny local cache so `scan` doesn't re-file the same events.
# This is NOT queue state (that's on Forgejo); it's the poller's memory of
# which mail ids it has already seen and when it last swept. Lives on the one
# host that runs `scan` (the cron container), never synced anywhere.
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


# --------------------------------------------------------------------------
# QueueClient — the Forgejo-label-backed queue. All claim/complete/list state
# lives on issues; this wraps ForgejoClient with the label bookkeeping.
# --------------------------------------------------------------------------

class QueueClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = client(cfg)
        self._base_ready = False
        self._claim_ready = set()

    # -- label plumbing --------------------------------------------------
    def _create_label(self, name, color, exclusive):
        try:
            self.client.create_label(name, color, exclusive=exclusive)
        except RuntimeError as e:
            if "already exists" not in str(e).lower():
                raise

    def _ensure_base(self):
        if self._base_ready:
            return
        existing = {l["name"] for l in self.client.list_labels()}
        for name, color, excl in (
            (JOB_LABEL, "ededed", False),
            (L_PENDING, "fbca04", True),
            (L_FAILED, "b60205", True),
        ):
            if name not in existing:
                self._create_label(name, color, excl)
        self._base_ready = True

    def _ensure_claim(self, worker):
        if worker in self._claim_ready:
            return
        self._create_label(CLAIM_PREFIX + worker, "0e8a16", True)
        self._claim_ready.add(worker)

    @staticmethod
    def has_lifecycle_label(labels):
        return any(
            l == L_PENDING or l == L_FAILED or l.startswith(CLAIM_PREFIX)
            for l in labels
        )

    # -- job body encoding (standalone hq-job tickets only) --------------
    @staticmethod
    def _job_body(job_type, target, payload, retries):
        meta = {"type": job_type, "target": str(target), "payload": payload or {},
                "retries": retries, "created": now_stamp()}
        return ("Automation job ticket managed by `hq queue`. Safe to close.\n\n"
                f"<!-- hq-job {json.dumps(meta)} -->\n")

    @staticmethod
    def _parse_job_body(body):
        m = _JOB_RE.search(body or "")
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None

    def _job_from_issue(self, issue):
        """Classify a Forgejo issue into a job dict. An hq-job ticket carries
        its type/target/payload in the body; any other lifecycle-labeled issue
        is an in-place collect on that GTD task."""
        labels = [l["name"] for l in (issue.get("labels") or [])]
        claim = next((l for l in labels if l.startswith(CLAIM_PREFIX)), None)
        base = {
            "carrier": issue["number"],
            "pending": L_PENDING in labels,
            "failed": L_FAILED in labels,
            "claimed_by": claim[len(CLAIM_PREFIX):] if claim else None,
            "updated_at": issue.get("updated_at"),
        }
        if JOB_LABEL in labels:
            meta = self._parse_job_body(issue.get("body")) or {}
            base.update(
                type=meta.get("type"), target=str(meta.get("target")),
                payload=meta.get("payload") or {}, retries=int(meta.get("retries", 0)),
                is_job_issue=True,
            )
        else:
            base.update(
                type="collect", target=str(issue["number"]),
                payload={"issue": issue["number"]}, retries=0, is_job_issue=False,
            )
        return base

    # -- producer side ---------------------------------------------------
    def enqueue(self, job_type, target, payload=None):
        """File a job. A plain collect labels the target task in place; every
        other type (and a forced collect, which must carry force=True) becomes
        a standalone hq-job ticket."""
        payload = payload or {}
        self._ensure_base()
        if job_type == "collect" and not payload.get("force"):
            issue = int(payload.get("issue") or target)
            self.client.add_labels(issue, [L_PENDING])  # exclusive: clears any claim
            return f"collect#{issue}"
        body = self._job_body(job_type, target, payload, retries=0)
        created = self.client.create_issue(
            f"[hq-job] {job_type}:{target}", body=body, labels=[JOB_LABEL, L_PENDING]
        )
        return f"{job_type}#{created.get('number')}"

    def existing_targets(self):
        """(type, target) pairs for open standalone hq-job tickets — the dedup
        set `scan` seeds so it never files a second triage/thread_update for a
        message already queued. In-place collect dedup is done inline in scan
        via each issue's own lifecycle label."""
        pairs = set()
        for i in self.client.list_issues(state="open", labels=JOB_LABEL):
            meta = self._parse_job_body(i.get("body"))
            if meta and meta.get("type") and meta.get("target") is not None:
                pairs.add((meta["type"], str(meta["target"])))
        return pairs

    # -- consumer side ---------------------------------------------------
    def list_pending(self):
        self._ensure_base()
        issues = self.client.list_issues(state="open", labels=L_PENDING)
        return [self._job_from_issue(i) for i in issues]

    def claim(self, job, worker):
        """Atomic claim: add queue/claimed:<worker> (exclusive scope removes
        queue/pending), then re-read and keep the job only if ours is the sole
        claim label — otherwise another worker won the race and we skip."""
        carrier = job["carrier"]
        claim_label = CLAIM_PREFIX + worker
        self._ensure_base()
        self._ensure_claim(worker)
        self.client.add_labels(carrier, [claim_label])
        labels = self.client.issue_labels(carrier)
        claims = [l for l in labels if l.startswith(CLAIM_PREFIX)]
        return claims == [claim_label]

    def finish(self, job, worker, ok):
        carrier = job["carrier"]
        claim_label = CLAIM_PREFIX + worker
        if ok:
            if job["is_job_issue"]:
                self.client.edit_issue(carrier, state="closed")
            else:
                # In-place collect done: drop the lifecycle label, leave the
                # task issue open.
                self.client.remove_label(carrier, claim_label)
            return "done"

        # failure
        if not job["is_job_issue"]:
            # A failed in-place collect just loses its label; the next scan
            # re-detects it if the dossier is still stale.
            self.client.remove_label(carrier, claim_label)
            return "requeued"
        retries = job.get("retries", 0) + 1
        if retries > MAX_RETRIES:
            self.client.add_labels(carrier, [L_FAILED])  # exclusive: clears claim
            self.client.edit_issue(carrier, state="closed")
            return "failed"
        self.client.edit_issue(
            carrier, body=self._job_body(job["type"], job["target"], job["payload"], retries)
        )
        self.client.add_labels(carrier, [L_PENDING])  # exclusive: clears claim
        return "requeued"

    def reclaim_stale(self, stale_seconds):
        """Return claimed jobs whose claim is older than the timeout back to
        pending so a dead/stuck worker can't strand them. Timestamp check is on
        the issue's updated_at (the claim label swap bumps it)."""
        if stale_seconds <= 0:
            return []
        cutoff = datetime.now(timezone.utc).timestamp() - stale_seconds
        reclaimed = []
        for i in self.client.list_issues(state="open"):
            labels = [l["name"] for l in (i.get("labels") or [])]
            if not any(l.startswith(CLAIM_PREFIX) for l in labels):
                continue
            ts = _parse_rfc3339(i.get("updated_at"))
            if ts is not None and ts < cutoff:
                self.client.add_labels(i["number"], [L_PENDING])  # exclusive: clears claim
                reclaimed.append(i["number"])
        return reclaimed

    def snapshot(self):
        """Live queue state from Forgejo for `hq queue list`."""
        self._ensure_base()
        pending, claimed, failed = [], [], []
        for i in self.client.list_issues(state="open"):
            labels = [l["name"] for l in (i.get("labels") or [])]
            if not (JOB_LABEL in labels or self.has_lifecycle_label(labels)):
                continue
            job = self._job_from_issue(i)
            desc = {"type": job["type"], "target": job["target"], "carrier": job["carrier"]}
            if L_PENDING in labels:
                pending.append(desc)
            if job["claimed_by"]:
                claimed.append({**desc, "worker": job["claimed_by"]})
            if L_FAILED in labels:
                failed.append(desc)
        return {"pending": pending, "claimed": claimed, "failed": failed}


# --------------------------------------------------------------------------
# scan — deterministic event detection (runs on the cron host)
# --------------------------------------------------------------------------

def scan_issues(cfg, state, q, taken):
    from .dossier import MARKER_PREFIX
    client = q.client
    user_login = owner(cfg)
    since = state.get("last_issue_scan")
    scan_started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    issues = client.list_issues(state="open", since=since)

    created = []
    for issue in issues:
        labels = [l["name"] for l in (issue.get("labels") or [])]
        if JOB_LABEL in labels:
            continue  # our own job tickets are not collect targets
        n = issue["number"]
        if ("collect", str(n)) in taken:
            continue
        if q.has_lifecycle_label(labels):
            taken.add(("collect", str(n)))  # a collect is already queued/claimed
            continue
        needs_collect = False
        if since is None or issue.get("created_at", "") > since:
            needs_collect = True
        else:
            for c in client.list_comments(n):
                body = c.get("body") or ""
                if body.startswith(MARKER_PREFIX):
                    continue
                if c.get("user", {}).get("login") == user_login:
                    needs_collect = True
                    break
        if needs_collect:
            created.append(q.enqueue("collect", str(n), {"issue": n}))
            taken.add(("collect", str(n)))

    state["last_issue_scan"] = scan_started
    return created


def cmd_scan(args):
    cfg = load_config()
    q = QueueClient(cfg)
    state = load_state(cfg)
    taken = q.existing_targets()

    from . import plugins

    created = []
    created += scan_issues(cfg, state, q, taken)
    for plugin in plugins.discover():
        plugin_scan = getattr(plugin, "scan", None)
        if plugin_scan:
            created += plugin_scan(cfg, state, q, taken) or []

    # Daily backstop: refresh dossiers on active tasks — one collect per issue.
    today = date.today().isoformat()
    if state.get("last_full_sweep") != today:
        from .task import fetch_tasks
        for t in fetch_tasks(cfg):
            if (t["status"] or "").lower() in ("next", "waiting") and t["issue"]:
                target = str(t["issue"])
                if ("collect", target) in taken:
                    continue
                if q.has_lifecycle_label(t.get("labels") or []):
                    taken.add(("collect", target))
                    continue
                created.append(q.enqueue("collect", target, {"issue": t["issue"]}))
                taken.add(("collect", target))
        state["last_full_sweep"] = today

    save_state(cfg, state)
    print_json({"created": created, "pending": len(q.snapshot()["pending"])})


# --------------------------------------------------------------------------
# work — claim + run jobs (local host, no ssh). Triage runs via a direct ollama
# call, collect via hermes; anything else falls to the run_job_llm stub.
# --------------------------------------------------------------------------

# Default local model names (qwen family) — overridable per job type in
# hq.yaml queue.runners.<type>.model. 4B for the single-shot triage transform,
# 27B for the agentic collect loop.
DEFAULT_TRIAGE_MODEL = "qwen3.5:4b"
DEFAULT_COLLECT_MODEL = "qwen3.6:27b"
DEFAULT_COLLECT_PROFILE = "hq-local"

# Structured-output schema the triage model MUST fill. ollama enforces it as
# the request `format`, so the CLI can trust the shape without re-prompting.
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["task", "noise"]},
        "task_title": {"type": "string"},
        "task_body": {"type": "string"},
        "draft_reply": {"type": "string"},
    },
    "required": ["category", "task_title", "task_body", "draft_reply"],
}


def runner_cfg(cfg, job_type):
    runners = queue_cfg(cfg).get("runners") or {}
    rc = runners.get(job_type) or {}
    default_model = {"triage": DEFAULT_TRIAGE_MODEL,
                     "collect": DEFAULT_COLLECT_MODEL}.get(job_type)
    return {
        "model": rc.get("model", default_model),
        "profile": rc.get("profile", DEFAULT_COLLECT_PROFILE),
        "prompt": rc.get("prompt", f"scripts/prompts/{job_type}.md"),
        "timeout_min": rc.get("timeout_min", 20),
    }


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


def _write_log(cfg, job_name, cmd, stdout, stderr, note=""):
    logs_dir = queue_dir(cfg) / "logs"
    logs_dir.mkdir(exist_ok=True)
    quoted = " ".join(shlex.quote(c) for c in cmd)
    (logs_dir / f"{job_name}.log").write_text(
        f"$ {quoted}\n{note}\n--- stdout ---\n{stdout or ''}\n"
        f"--- stderr ---\n{stderr or ''}"
    )


# --------------------------------------------------------------------------
# Triage runner — direct ollama, NO agent. One structured-output call per
# email; the CLI applies the result deterministically. Testable with a fixture
# email via classify_email() (which is the only part that touches the model).
# --------------------------------------------------------------------------

def classify_email(cfg, email, model=None):
    """The model half of triage: hand ollama the triage prompt + the email text
    and get back the enforced {category, task_title, task_body, draft_reply}.
    `email` is the dict from mail.read_message (from/subject/date/body/...).
    Deterministic and side-effect-free, so a fixture email can exercise it."""
    rc = runner_cfg(cfg, "triage")
    model = model or rc["model"]
    prompt = render_prompt(rc["prompt"], {
        "from": email.get("from", ""),
        "subject": email.get("subject", ""),
        "date": email.get("date", ""),
        "body": email.get("body", ""),
    })
    result = ollama.generate_json(
        prompt, TRIAGE_SCHEMA, model, cfg=cfg,
        timeout=rc["timeout_min"] * 60,
    )
    if result.get("category") not in ("task", "noise"):
        raise RuntimeError(f"triage model returned bad category: {result!r}")
    return result


def _hq_cli_json(args):
    """Run `./bin/hq …` in-repo and parse its JSON stdout. Reuses the exact CLI
    entry points (task add / mail draft-reply) so triage applies results through
    the same validated code paths a human would."""
    hq = str(repo_root() / "bin" / "hq")
    proc = subprocess.run([hq, *args], capture_output=True, text=True,
                          cwd=str(repo_root()))
    if proc.returncode != 0:
        raise RuntimeError(
            f"`hq {' '.join(args)}` failed: "
            f"{(proc.stderr or proc.stdout).strip()[:400]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"raw": proc.stdout.strip()}


def run_triage_job(cfg, payload, log, job_name):
    """Turn one unread email into an Inbox task card (+ optional draft reply)
    via a single direct ollama call. Noise is dropped. We fetch the email and
    apply the result; the model only classifies."""
    from .plugins.mail import read_message, thread_marker

    mid = payload.get("message_id")
    tid = payload.get("thread_id")
    if not mid:
        return False, "triage job missing message_id"

    try:
        email = read_message(cfg, mid)
    except Exception as e:  # gws/network failure — requeue
        return False, f"could not read email {mid}: {e}"

    try:
        result = classify_email(cfg, email)
    except RuntimeError as e:
        _write_log(cfg, job_name, ["ollama", "triage", mid], "", str(e),
                   note="triage model call failed")
        return False, str(e)

    category = result["category"]
    if category == "noise":
        log.append({"job": job_name, "category": "noise", "message_id": mid})
        return True, None

    title = (result.get("task_title") or "").strip() or email.get("subject") or "Follow up on email"
    body_lines = [(result.get("task_body") or "").strip(),
                  f"\nFrom: {email.get('from', 'unknown')}",
                  f"Email: {email.get('url', '')}"]
    if tid:
        # Bind this Gmail thread to the issue so later replies log here instead
        # of spawning a duplicate card. The marker convention lives in the mail
        # plugin; we ask it for the exact string rather than hardcoding it.
        body_lines.append(thread_marker(tid))
    notes = "\n".join(line for line in body_lines if line is not None)

    params_file = queue_dir(cfg) / "out" / f"triage-{mid}.json"
    params_file.parent.mkdir(exist_ok=True)
    params_file.write_text(json.dumps({"title": title, "notes": notes}))
    try:
        added = _hq_cli_json(["task", "add", str(params_file)])
    except RuntimeError as e:
        return False, str(e)
    finally:
        params_file.unlink(missing_ok=True)

    outcome = {"job": job_name, "category": "task",
               "issue": added.get("issue"), "message_id": mid}

    draft = (result.get("draft_reply") or "").strip()
    if draft:
        reply_file = queue_dir(cfg) / "out" / f"triage-reply-{mid}.txt"
        reply_file.write_text(draft)
        try:
            _hq_cli_json(["mail", "draft-reply", mid, "--body-file", str(reply_file)])
            outcome["draft_reply"] = True
        except RuntimeError as e:
            # The task card exists — a failed draft shouldn't fail the job or
            # trigger a retry that re-files the card. Note it and move on.
            outcome["draft_reply_error"] = str(e)
        finally:
            reply_file.unlink(missing_ok=True)

    log.append(outcome)
    return True, None


# --------------------------------------------------------------------------
# Collect runner — hermes agent loop (27B). Multi-step tool use across
# mail/drive/wiki, so it IS agentic. Hermes memory on (27B handles writes).
# --------------------------------------------------------------------------

def run_hermes_collect(cfg, payload, log, job_name):
    """Drive `hermes -z -p hq-local` with the collect prompt. Hermes reaches
    ollama via the hq-local profile config (base_url http://ollama:11434); the
    model comes from hq.yaml queue.runners.collect.model. The model writes the
    dossier JSON to out_file; run_collect_job validates and posts it."""
    rc = runner_cfg(cfg, "collect")
    prompt = render_prompt(rc["prompt"], payload)
    cmd = ["hermes", "-z", prompt, "-p", rc["profile"]]
    if rc["model"]:
        cmd += ["-m", rc["model"]]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=rc["timeout_min"] * 60, cwd=str(repo_root()),
        )
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        _write_log(cfg, job_name, cmd, out, err,
                   note=f"TIMED OUT after {rc['timeout_min']} min")
        return False, f"hermes timed out after {rc['timeout_min']} min"
    except FileNotFoundError:
        return False, "hermes is not installed on this host"

    _write_log(cfg, job_name, cmd, proc.stdout, proc.stderr, note=f"exit {proc.returncode}")
    log.append({"cmd": "hermes -z … -p " + rc["profile"], "exit": proc.returncode,
                "tail": (proc.stdout + proc.stderr)[-500:]})
    if proc.returncode != 0:
        return False, f"hermes exited {proc.returncode}"
    return True, None


def run_job_llm(cfg, job_type, payload, log, job_name="job"):
    # Fallback for any job type that is neither collect (hermes) nor triage
    # (direct ollama) and that no plugin's handle_job() claimed. There is no
    # generic agentic runner by design — such a job is a scheduling bug.
    log.append({"cmd": f"run_job_llm({job_type}) — no runner for this type",
                "exit": None, "tail": ""})
    return False, f"no runner configured for job type {job_type!r}"


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

    ok, err = run_hermes_collect(cfg, {"issue": issue, "out_file": str(out_file)},
                                 log, job_name)
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
    q = QueueClient(cfg)
    worker = worker_name()
    types = [t.strip() for t in args.types.split(",")] if args.types else None

    stale_s = int(queue_cfg(cfg).get("stale_min", 60)) * 60
    reclaimed = q.reclaim_stale(stale_s)

    processed = []
    log = []
    for job in q.list_pending():
        if len(processed) >= args.max:
            break
        job_type = job["type"]
        if types and job_type not in types:
            continue
        if not q.claim(job, worker):
            continue  # another worker won the race
        target = job["target"]
        payload = job.get("payload") or {}
        job_name = f"{job_type}-{target}"
        if job_type == "collect":
            ok, err = run_collect_job(cfg, payload.get("issue") or int(target), log,
                                      job_name=job_name, force=payload.get("force", False))
        elif job_type == "triage":
            ok, err = run_triage_job(cfg, payload, log, job_name)
        else:
            result = _dispatch_to_plugin(job_type, cfg, payload, log, job_name)
            if result is not None:
                ok, err = result
            else:
                ok, err = run_job_llm(cfg, job_type, payload, log, job_name=job_name)
        outcome = q.finish(job, worker, ok)
        processed.append({"type": job_type, "target": target, "carrier": job["carrier"],
                          "outcome": outcome, **({"error": err} if err else {})})

    print_json({"worker": worker, "reclaimed": reclaimed, "processed": processed,
                "log": log if args.verbose else []})


def cmd_list(args):
    cfg = load_config()
    q = QueueClient(cfg)
    print_json(q.snapshot())


def cmd_add(args):
    cfg = load_config()
    q = QueueClient(cfg)
    payload = {}
    target = args.target or args.type
    if args.issue:
        payload["issue"] = args.issue
        target = str(args.issue)
    ref = q.enqueue(args.type, target, payload)
    print_json({"created": ref})


def register(sub):
    p = sub.add_parser("queue", help="event detection + job queue (automation plumbing)")
    s2 = p.add_subparsers(dest="queue_cmd", required=True)

    s = s2.add_parser("scan", help="detect new mail/issue activity and enqueue jobs as Forgejo labels (runs on the cron host)")
    s.set_defaults(func=cmd_scan)

    s = s2.add_parser("work", help="claim pending jobs (atomic label swap) and run them with the configured runner")
    s.add_argument("--types", help="comma-separated job types to handle (e.g. triage or collect,thread_update)")
    s.add_argument("--max", type=int, default=10)
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_work)

    s2.add_parser("list", help="live queue state (pending/claimed/failed) from Forgejo").set_defaults(func=cmd_list)

    s = s2.add_parser("add", help="manually enqueue a job (testing)")
    s.add_argument("type", choices=["triage", "collect"])
    s.add_argument("--issue", type=int)
    s.add_argument("--target")
    s.set_defaults(func=cmd_add)
