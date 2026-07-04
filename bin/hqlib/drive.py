"""hq drive — read-only Google Drive search + excerpt via `gws`.

Uses the same account/config-dir mapping as mail (gmail_triage.accounts).
No write verbs by design.
"""
import json

from .common import fail, run_json, load_config, excerpt, envelope, print_json
from .mail import resolve_account, gws_env

MIME_BY_TYPE = {
    "doc": "application/vnd.google-apps.document",
    "sheet": "application/vnd.google-apps.spreadsheet",
    "slides": "application/vnd.google-apps.presentation",
    "pdf": "application/pdf",
    "folder": "application/vnd.google-apps.folder",
}


def _escape(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def cmd_find(args):
    cfg = load_config()
    account, config_dir, _ = resolve_account(cfg, args.account)
    q_parts = ["trashed = false"]
    if args.text:
        q_parts.append(f"fullText contains '{_escape(args.text)}'")
    if args.name:
        q_parts.append(f"name contains '{_escape(args.name)}'")
    if args.type and args.type != "any":
        mime = MIME_BY_TYPE.get(args.type)
        if not mime:
            fail(f"--type must be one of {sorted(MIME_BY_TYPE)} or 'any'")
        q_parts.append(f"mimeType = '{mime}'")
    if not args.text and not args.name:
        fail("find needs --text and/or --name")

    data = run_json([
        "gws", "drive", "files", "list", "--params",
        json.dumps({
            "q": " and ".join(q_parts),
            "pageSize": args.max,
            "orderBy": "modifiedTime desc",
            "fields": "files(id,name,mimeType,modifiedTime,webViewLink,owners(emailAddress))",
        }),
    ], env=gws_env(config_dir))

    results = [
        {
            "id": f.get("id"),
            "name": f.get("name"),
            "type": f.get("mimeType", "").removeprefix("application/vnd.google-apps.").removeprefix("application/"),
            "modified": (f.get("modifiedTime") or "")[:10],
            "url": f.get("webViewLink"),
        }
        for f in data.get("files", [])
    ]
    out = envelope(results)
    out["account"] = account
    print_json(out)


def _doc_text(doc):
    """Flatten a Docs API document resource to plain text."""
    parts = []

    def walk_elements(elements):
        for el in elements:
            para = el.get("paragraph")
            if para:
                for pe in para.get("elements", []):
                    text = (pe.get("textRun") or {}).get("content")
                    if text:
                        parts.append(text)
            table = el.get("table")
            if table:
                for row in table.get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk_elements(cell.get("content", []))

    walk_elements(doc.get("body", {}).get("content", []))
    return "".join(parts)


def cmd_excerpt(args):
    cfg = load_config()
    account, config_dir, _ = resolve_account(cfg, args.account)
    env = gws_env(config_dir)

    meta = run_json([
        "gws", "drive", "files", "get", "--params",
        json.dumps({"fileId": args.file_id, "fields": "id,name,mimeType,webViewLink"}),
    ], env=env)
    mime = meta.get("mimeType", "")

    if mime == MIME_BY_TYPE["doc"]:
        doc = run_json([
            "gws", "docs", "documents", "get", "--params",
            json.dumps({"documentId": args.file_id}),
        ], env=env)
        text = _doc_text(doc)
    else:
        fail(
            f"excerpt supports Google Docs only for now (this file is {mime or 'unknown'}). "
            "Link the file in the dossier and read it in the browser."
        )

    if args.query:
        idx = text.lower().find(args.query.lower())
        if idx >= 0:
            half = args.max_chars // 2
            window = text[max(0, idx - half): idx + half]
        else:
            window = text[: args.max_chars]
    else:
        window = text[: args.max_chars]

    print_json({
        "id": args.file_id,
        "name": meta.get("name"),
        "url": meta.get("webViewLink"),
        "account": account,
        "query": args.query,
        "query_found": (args.query.lower() in text.lower()) if args.query else None,
        "total_chars": len(text),
        "excerpt": excerpt(window, args.max_chars),
    })


def register(sub):
    p = sub.add_parser("drive", help="Google Drive (read-only: find + excerpt)")
    s2 = p.add_subparsers(dest="drive_cmd", required=True)

    s = s2.add_parser("find", help="search files by content and/or name")
    s.add_argument("--text", help="full-text search inside files")
    s.add_argument("--name", help="words in the file name")
    s.add_argument("--type", choices=sorted(MIME_BY_TYPE) + ["any"], default="any")
    s.add_argument("--account")
    s.add_argument("--max", type=int, default=8)
    s.set_defaults(func=cmd_find)

    s = s2.add_parser("excerpt", help="plain-text excerpt of a Google Doc (window around --query if given)")
    s.add_argument("file_id")
    s.add_argument("--query", help="center the excerpt on this phrase")
    s.add_argument("--max-chars", dest="max_chars", type=int, default=600)
    s.add_argument("--account")
    s.set_defaults(func=cmd_excerpt)
