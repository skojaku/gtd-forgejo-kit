"""ollama.py — stdlib-only client for the local ollama server.

Used by the triage runner: triage is a single deterministic transformation
(one email in -> {category, task_title, task_body, draft_reply} out), not
agentic work, so it does NOT go through hermes. It is one direct HTTP call to
ollama's /api/generate with a forced JSON schema ("structured outputs"), and
nothing else — no soul file, no memory, no tool schemas. The 4B model sees only
the triage prompt + the email text and MUST emit the four fields.

House style mirrors forgejo.py: plain `urllib.request`, a small wrapper, a clear
`RuntimeError` on failure.

Base URL resolution (in order):
  1. $OLLAMA_URL env var (compose/container override)
  2. cfg queue.ollama_url (config/hq.yaml)
  3. http://ollama:11434 (the compose service-name default)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_URL = "http://ollama:11434"


def base_url(cfg=None) -> str:
    env = os.environ.get("OLLAMA_URL")
    if env and env.strip():
        return env.strip().rstrip("/")
    if cfg:
        url = ((cfg.get("queue") or {}).get("ollama_url") or "").strip()
        if url:
            return url.rstrip("/")
    return DEFAULT_URL


def list_models(cfg=None, url=None, timeout=5):
    """GET /api/tags — confirms ollama is up and lists installed models. Returns
    the list of model-name strings. Raises RuntimeError on transport failure.
    Read-only; used by `hq doctor`."""
    url = url or base_url(cfg)
    req = urllib.request.Request(f"{url}/api/tags", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ollama /api/tags -> {e.code}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"ollama /api/tags at {url} failed: {e}") from None
    return [m.get("name") for m in (resp.get("models") or [])]


def generate_json(prompt, schema, model, *, cfg=None, url=None, system=None,
                  timeout=120):
    """One structured-output /api/generate call. `schema` is a JSON Schema dict
    passed as ollama's `format`, which constrains the model to emit conforming
    JSON. Returns the parsed object. Raises RuntimeError on transport failure or
    if the response is not the promised JSON."""
    url = url or base_url(cfg)
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": schema,
        # Disable reasoning: hybrid "thinking" models (e.g. qwen3.x) otherwise
        # spend the turn in a `thinking` field and leave `response` empty, which
        # breaks structured output. Triage is a classification, not a reasoning
        # task, so we want the JSON directly.
        "think": False,
        # Deterministic: triage is a classification, not a creative task.
        "options": {"temperature": 0},
    }
    if system:
        payload["system"] = system
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{url}/api/generate", data=data, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        raise RuntimeError(f"ollama /api/generate -> {e.code}: {detail[:400]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"ollama /api/generate at {url} failed: {e}") from None
    text = resp.get("response", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"ollama returned non-JSON despite structured-output schema: "
            f"{text[:300]!r} ({e})"
        )
