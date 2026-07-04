"""hq cal — Google Calendar via `gws`, with all scheduling policy in code.

Policy (from config/env.yaml working_hours): weekday blocks, commute gap
(never bookable, splits blocks), max_bookable_pct and min_free_min
(treat-time). Callers get policy-aware answers — no prose reasoning needed.

`book` writes real events and is approval-gated: params must carry
"approved": true, set only after the user has explicitly approved the
sessions in conversation.
"""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ...common import (
    fail, run, run_json, load_config,
    envelope, print_json, read_params, is_valid_date,
)

NAME = "cal"

WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_hhmm(s):
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def minutes_to_hhmm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


# --------------------------------------------------------------------------
# Working-hour blocks + commute policy
# --------------------------------------------------------------------------

def get_blocks(cfg, weekday_name):
    raw = (cfg.get("working_hours") or {}).get(weekday_name, []) or []
    blocks = []
    for entry in raw:
        s, e = entry.split("-")
        blocks.append((parse_hhmm(s), parse_hhmm(e)))
    return sorted(blocks)


def commute_gap(cfg):
    commute = (cfg.get("working_hours") or {}).get("commute") or {}
    cs, ce = commute.get("start"), commute.get("end")
    if not cs or not ce:
        return None
    return parse_hhmm(cs), parse_hhmm(ce)


def split_by_commute(blocks, cfg):
    gap = commute_gap(cfg)
    if not gap:
        return blocks
    cs, ce = gap
    result = []
    for bs, be in blocks:
        if bs < cs and be > ce:
            result.append((bs, cs))
            result.append((ce, be))
        elif bs < ce and be > cs:
            if bs < cs:
                result.append((bs, cs))
            if be > ce:
                result.append((ce, be))
        else:
            result.append((bs, be))
    return result


def working_blocks(cfg, weekday_name):
    """Blocks with the commute gap carved out — the only blocks used for scheduling."""
    return split_by_commute(get_blocks(cfg, weekday_name), cfg)


def classify_suitability(start_min):
    if start_min < 7 * 60:
        return "LOW"
    if start_min < 12 * 60:
        return "HIGH"
    return "MEDIUM"


def treat_time_warnings(cfg, total_min, booked_min, proposed_min):
    wh = cfg.get("working_hours") or {}
    max_pct = wh.get("max_bookable_pct", 80)
    min_free = wh.get("min_free_min", 30)
    if total_min == 0:
        return []
    after = booked_min + proposed_min
    util = after / total_min * 100
    free_after = total_min - after
    warnings = []
    if util > max_pct:
        warnings.append(f"utilization {util:.0f}% exceeds cap of {max_pct}%")
    if free_after < min_free:
        warnings.append(f"only {free_after} min free (minimum {min_free} min)")
    return warnings


# --------------------------------------------------------------------------
# Event fetch
# --------------------------------------------------------------------------

def tz_name(cfg):
    return (cfg.get("user") or {}).get("timezone", "UTC")


def rfc3339_midnight(day_str, tz):
    y, m, d = (int(x) for x in day_str.split("-"))
    return datetime(y, m, d, tzinfo=ZoneInfo(tz)).isoformat()


def fetch_events(cfg, frm, to, calendar_id="primary"):
    tz = tz_name(cfg)
    time_min = rfc3339_midnight(frm, tz)
    day_after_to = (date.fromisoformat(to) + timedelta(days=1)).isoformat()
    time_max = rfc3339_midnight(day_after_to, tz)
    events_data = run_json([
        "gws", "calendar", "events", "list", "--params",
        json.dumps({
            "calendarId": calendar_id,
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 2500,
        }),
    ])
    events = events_data.get("items") if isinstance(events_data, dict) else events_data
    return events if isinstance(events, list) else []


def busy_intervals_for_day(events, day_str, tz):
    """(start_min, end_min) local intervals for events overlapping day_str."""
    zone = ZoneInfo(tz)
    day_start = datetime.fromisoformat(day_str).replace(tzinfo=zone)
    day_end = day_start + timedelta(days=1)
    busy = []
    for ev in events:
        if ev.get("status") == "cancelled":
            continue
        start, end = ev.get("start", {}), ev.get("end", {})
        if start.get("date") and not start.get("dateTime"):
            if start["date"] <= day_str < (end.get("date") or start["date"]):
                busy.append((0, 24 * 60))
            continue
        s_dt, e_dt = start.get("dateTime"), end.get("dateTime")
        if not s_dt or not e_dt:
            continue
        try:
            s_local = datetime.fromisoformat(s_dt).astimezone(zone)
            e_local = datetime.fromisoformat(e_dt).astimezone(zone)
        except ValueError:
            continue
        if e_local <= day_start or s_local >= day_end:
            continue
        s_local = max(s_local, day_start)
        e_local = min(e_local, day_end)
        busy.append((s_local.hour * 60 + s_local.minute, e_local.hour * 60 + e_local.minute))
    return sorted(busy)


def free_slots_in_blocks(blocks, busy):
    free = []
    for bs, be in blocks:
        cur = bs
        overlapping = sorted(iv for iv in busy if iv[1] > cur and iv[0] < be)
        for s, e in overlapping:
            if s > cur:
                free.append((cur, min(s, be)))
            cur = max(cur, e)
            if cur >= be:
                break
        if cur < be:
            free.append((cur, be))
    return [(s, e) for s, e in free if e > s]


def compute_booked(busy, blocks):
    total = 0
    for es, ee in busy:
        for bs, be in blocks:
            lo, hi = max(es, bs), min(ee, be)
            if lo < hi:
                total += hi - lo
    return total


# --------------------------------------------------------------------------
# Day budgets (shared by free / propose*)
# --------------------------------------------------------------------------

def compute_day_budgets(cfg, frm, to):
    events = fetch_events(cfg, frm, to)
    wh = cfg.get("working_hours") or {}
    max_pct = wh.get("max_bookable_pct", 80) / 100
    min_free = wh.get("min_free_min", 30)
    tz = tz_name(cfg)

    days = []
    cur = date.fromisoformat(frm)
    end = date.fromisoformat(to)
    while cur <= end:
        day_str = cur.isoformat()
        wname = WEEKDAY_NAMES[cur.weekday()]
        blocks = working_blocks(cfg, wname)
        total_working = sum(e - s for s, e in blocks)
        if total_working > 0:
            busy = busy_intervals_for_day(events, day_str, tz)
            free = free_slots_in_blocks(blocks, busy)
            free_total = sum(e - s for s, e in free)
            already_booked = total_working - free_total
            cap = max(0, total_working * max_pct - already_booked)
            available = min(free_total, cap)
            skip = available < min_free or available <= 0
            days.append({
                "date": day_str,
                "weekday": wname,
                "free_slots_min": free,
                "available_to_book_min": 0 if skip else available,
                "skip": skip,
            })
        cur += timedelta(days=1)
    return days


def allocate_sessions(days, minutes_needed, min_session):
    """Greedy allocator; consumes day budgets in place (see gtd heritage)."""
    sessions = []
    remaining = minutes_needed
    for day in days:
        if remaining <= 0:
            break
        if day["skip"] or day["available_to_book_min"] <= 0 or not day["free_slots_min"]:
            continue
        slots = day["free_slots_min"]
        best_idx = max(range(len(slots)), key=lambda i: slots[i][1] - slots[i][0])
        s, e = slots[best_idx]
        session_len = min(remaining, e - s, day["available_to_book_min"])
        if session_len < min_session and remaining >= min_session:
            continue
        if session_len <= 0:
            continue
        session_len = int(round(session_len))
        sessions.append({
            "date": day["date"], "start": minutes_to_hhmm(s),
            "end": minutes_to_hhmm(s + session_len), "minutes": session_len,
        })
        remaining -= session_len
        day["available_to_book_min"] -= session_len
        new_start = s + session_len
        slots[best_idx] = (new_start, e) if new_start < e else None
        day["free_slots_min"] = [sl for sl in slots if sl is not None]
    return sessions, remaining


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_agenda(args):
    cfg = load_config()
    today = date.today().isoformat()
    to = (date.today() + timedelta(days=args.days - 1)).isoformat()
    events = fetch_events(cfg, today, to)
    results = []
    for ev in events:
        if ev.get("status") == "cancelled":
            continue
        start, end = ev.get("start", {}), ev.get("end", {})
        results.append({
            "summary": ev.get("summary"),
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "location": ev.get("location"),
            "id": ev.get("id"),
            "url": ev.get("htmlLink"),
        })
    out = envelope(results[: args.max], len(results))
    out["from"] = today
    out["to"] = to
    print_json(out)


def cmd_free(args):
    cfg = load_config()
    if not is_valid_date(args.frm) or not is_valid_date(args.to):
        fail("--from and --to must be YYYY-MM-DD")
    days = compute_day_budgets(cfg, args.frm, args.to)
    print_json([{
        "date": d["date"],
        "weekday": d["weekday"],
        "free_slots": [f"{minutes_to_hhmm(s)}-{minutes_to_hhmm(e)}" for s, e in d["free_slots_min"]],
        "free_total_min": sum(e - s for s, e in d["free_slots_min"]),
        "available_to_book_min": round(d["available_to_book_min"]),
        "skip": d["skip"],
    } for d in days])


def cmd_check(args):
    """Validate one proposed time: working hours, commute, conflicts, treat-time."""
    cfg = load_config()
    if not is_valid_date(args.date):
        fail("--date must be YYYY-MM-DD")
    tz = tz_name(cfg)
    weekday = WEEKDAY_NAMES[date.fromisoformat(args.date).weekday()]
    req_start, req_end = parse_hhmm(args.start), parse_hhmm(args.end)
    if req_end <= req_start:
        fail("--end must be after --start")
    proposed_min = req_end - req_start

    raw_blocks = get_blocks(cfg, weekday)
    blocks = split_by_commute(raw_blocks, cfg)
    total_min = sum(e - s for s, e in blocks)

    events = fetch_events(cfg, args.date, args.date)
    busy = busy_intervals_for_day(events, args.date, tz)
    booked_min = compute_booked(busy, blocks)

    warnings = []
    if raw_blocks:
        covered = sum(
            max(0, min(req_end, be) - max(req_start, bs)) for bs, be in raw_blocks
        )
        if covered < proposed_min:
            block_strs = [f"{minutes_to_hhmm(s)}-{minutes_to_hhmm(e)}" for s, e in raw_blocks]
            warnings.append({
                "type": "outside_hours",
                "message": f"{args.start}-{args.end} is partially or fully outside working hours ({', '.join(block_strs)})",
            })
    else:
        warnings.append({"type": "no_working_hours", "message": f"no working hours defined for {weekday}"})

    gap = commute_gap(cfg)
    if gap and req_start < gap[1] and req_end > gap[0]:
        warnings.append({
            "type": "commute_gap",
            "message": f"{args.start}-{args.end} crosses the commute gap ({minutes_to_hhmm(gap[0])}-{minutes_to_hhmm(gap[1])})",
        })

    for ev in events:
        if ev.get("status") == "cancelled":
            continue
        start = ev.get("start", {})
        if start.get("date") and not start.get("dateTime"):
            continue
        for es, ee in busy_intervals_for_day([ev], args.date, tz):
            if req_start < ee and req_end > es:
                warnings.append({
                    "type": "conflict",
                    "message": f"overlaps '{ev.get('summary', 'Untitled')}' {minutes_to_hhmm(es)}-{minutes_to_hhmm(ee)}",
                })

    for w in treat_time_warnings(cfg, total_min, booked_min, proposed_min):
        warnings.append({"type": "treat_time", "message": w})

    booked_after = booked_min + proposed_min
    print_json({
        "ok": not warnings,
        "warnings": warnings,
        "suitability": classify_suitability(req_start),
        "day_impact": {
            "total_working_min": total_min,
            "booked_min": booked_min,
            "proposed_min": proposed_min,
            "booked_after": booked_after,
            "free_after": total_min - booked_after,
            "utilization_pct": round(booked_after / total_min * 100, 1) if total_min else 0,
        },
    })


def cmd_propose(args):
    cfg = load_config()
    params = read_params(args.file)
    frm, to = params.get("from"), params.get("to")
    min_session = params.get("min_session", 25)
    if not (frm and to and is_valid_date(frm) and is_valid_date(to)):
        fail("params must have 'from' and 'to' as YYYY-MM-DD")

    if params.get("minutes"):
        minutes = params["minutes"]
        if not isinstance(minutes, int) or minutes <= 0:
            fail("'minutes' must be a positive integer")
        days = compute_day_budgets(cfg, frm, to)
        sessions, remaining = allocate_sessions(days, minutes, min_session)
        print_json({
            "sessions": sessions,
            "requested_min": minutes,
            "allocated_min": minutes - remaining,
            "shortfall_min": remaining,
            "fits": remaining <= 0,
        })
        return

    from .task import fetch_tasks
    if params.get("project"):
        tasks = [
            t for t in fetch_tasks(cfg)
            if (t["project"] or "").lower() == params["project"].lower()
            and t["booked"] is None and t["status"] != "Done"
        ]
    elif params.get("issues"):
        wanted = set(params["issues"])
        tasks = [t for t in fetch_tasks(cfg) if t["issue"] in wanted]
    else:
        fail("params must have 'minutes', 'project', or 'issues'")

    tasks.sort(key=lambda t: (t["due"] or "9999-99-99", t["duration"] or 0, t["title"] or ""))
    days = compute_day_budgets(cfg, frm, to)

    results = []
    total_requested = total_allocated = 0
    for t in tasks:
        if not t["duration"]:
            results.append({"issue": t["issue"], "title": t["title"], "skipped": "no duration set"})
            continue
        sessions, remaining = allocate_sessions(days, t["duration"], min_session)
        allocated = t["duration"] - remaining
        total_requested += t["duration"]
        total_allocated += allocated
        results.append({
            "issue": t["issue"], "title": t["title"], "sessions": sessions,
            "requested_min": t["duration"], "allocated_min": allocated, "shortfall_min": remaining,
        })

    print_json({
        "tasks": results,
        "totals": {"requested_min": total_requested, "allocated_min": total_allocated,
                   "shortfall_min": total_requested - total_allocated},
        "deadline_met": total_allocated >= total_requested,
    })


def cmd_book(args):
    cfg = load_config()
    params = read_params(args.file)

    if params.get("approved") is not True:
        fail(
            'booking requires "approved": true in the params file — set it only '
            "after the user has explicitly approved these exact sessions."
        )

    issue = params.get("issue")
    sessions = params.get("sessions")
    if not issue or not sessions:
        fail("params must have 'issue' and a non-empty 'sessions' list")

    from .task import find_item_id, edit_date
    item_id, task = find_item_id(cfg, issue)
    title = task["title"]
    tz = tz_name(cfg)
    calendar_id = params.get("calendar_id", "primary")

    created = []
    for s in sessions:
        d, start, end = s.get("date"), s.get("start"), s.get("end")
        if not (d and start and end):
            fail(f"each session needs date/start/end, got: {s}")
        run([
            "gws", "calendar", "events", "insert",
            "--params", json.dumps({"calendarId": calendar_id}),
            "--json", json.dumps({
                "summary": title,
                "start": {"dateTime": f"{d}T{start}:00", "timeZone": tz},
                "end": {"dateTime": f"{d}T{end}:00", "timeZone": tz},
            }),
        ])
        created.append(s)

    first_date = sessions[0]["date"]
    edit_date(cfg, item_id, "scheduled", first_date)
    edit_date(cfg, item_id, "booked", first_date)

    print_json({
        "issue": issue, "title": title, "sessions_created": created,
        "calendar_id": calendar_id,
        "scheduled": first_date, "booked": first_date,
    })


def register(sub):
    p = sub.add_parser("cal", help="calendar (policy-aware; booking needs explicit user approval)")
    s2 = p.add_subparsers(dest="cal_cmd", required=True)

    s = s2.add_parser("agenda", help="upcoming events, small projection")
    s.add_argument("--days", type=int, default=1)
    s.add_argument("--max", type=int, default=15)
    s.set_defaults(func=cmd_agenda)

    s = s2.add_parser("free", help="free slots per day, net of working hours/commute/treat-time")
    s.add_argument("--from", dest="frm", required=True)
    s.add_argument("--to", required=True)
    s.set_defaults(func=cmd_free)

    s = s2.add_parser("check", help="validate a proposed time (hours, commute, conflicts, treat-time)")
    s.add_argument("--date", required=True)
    s.add_argument("--start", required=True, help="HH:MM")
    s.add_argument("--end", required=True, help="HH:MM")
    s.set_defaults(func=cmd_check)

    s = s2.add_parser("propose", help="propose sessions from a JSON params file: {from, to, minutes|project|issues, min_session?}")
    s.add_argument("file")
    s.set_defaults(func=cmd_propose)

    s = s2.add_parser("book", help='create events + set Scheduled/Booked — params must include "approved": true')
    s.add_argument("file")
    s.set_defaults(func=cmd_book)
