#!/usr/bin/env python3
"""gate.py: a free, model-less scan of what your scheduled Claude Code agents did since the last run.

Sources:
  * scheduled-run transcripts in ~/.claude/projects (final report, tool errors, permission denials)
  * the Claude app's own scheduled-task store (cron, enabled, lastRunAt) to find slots that never started
  * alerts that agents append to ~/.claude/agent-manager/alerts.jsonl (see alert.py)

It writes <state-dir>/events.json. The control-tower agent only reasons about events whose score is at or
above THRESHOLD, so a quiet fleet costs no tokens.

    python3 gate.py                  # real run, stamps last_run at the start
    python3 gate.py --state-dir X    # dry run against a scratch state dir

Exit code 3 (OVERLAP) means another gate run started less than 20 minutes ago: stop, write nothing.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
PROJECTS = HOME / ".claude" / "projects"
STATE_DIR = HOME / ".claude" / "agent-manager"
ALERTS = STATE_DIR / "alerts.jsonl"

THRESHOLD = 3
SELF = os.environ.get("FLEET_SELF_TASK", "agent-manager")   # the control tower's own task id
FIRST_LOOKBACK = timedelta(days=7)
# Re-enabling a task fires its missed slot alongside "Run now": two runs seconds apart.
# The later one must not re-scan (it would re-report the same events).
OVERLAP = timedelta(minutes=20)
EXIT_OVERLAP = 3

TASK_RE = re.compile(r'<scheduled-task name="([^"]+)"')
# The harness's own wording on a denied tool call. Matched only on error results: agents
# routinely read notes that quote "Blocked by classifier".
DENIAL_RE = re.compile(r"Permission for this action was denied|doesn't want to proceed", re.I)
# Delivery failures only. Narration like "couldn't confirm the case size" is normal.
TROUBLE_RE = re.compile(
    r"logged[- ]out|QR (screen|code)|not delivered|nothing (was )?sent|did(n't| not) send"
    r"|could(n't| not) (send|deliver|post)|failed to (send|deliver|post)", re.I)
NOISY_ERRORS = 10  # browser agents hit 3-6 tool errors on a normal run
ALERT_SCORE = {"failed": 5, "needs-human": 5, "info": 1}


def load_areas(state_dir):
    """Optional <state-dir>/areas.json: {"seo-": "websites", "bw-social": "social"}. First matching prefix wins."""
    try:
        return list(json.loads((Path(state_dir) / "areas.json").read_text()).items())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


AREAS = load_areas(STATE_DIR)


def area_of(task):
    for prefix, area in AREAS:
        if (task or "").startswith(prefix):
            return area
    return "other"


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def read_run(path):
    """Summarise a scheduled-task transcript; None if the session is not a scheduled run."""
    task = started = ended = None
    report, errors, denials = "", 0, 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("timestamp"):
                ended = parse_ts(row["timestamp"])
                started = started or ended
            if task is None and row.get("type") == "queue-operation":
                m = TASK_RE.search(row.get("content") or "")
                if m:
                    task = m.group(1)
            content = (row.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    if block.get("is_error"):
                        errors += 1
                        denials += bool(DENIAL_RE.search(json.dumps(block.get("content"))))
                elif block.get("type") == "text" and row.get("type") == "assistant":
                    report = block.get("text", "")
    if task is None:
        return None
    return {"task": task, "session": Path(path).stem,
            "started": started.isoformat() if started else None,
            "ended": ended.isoformat() if ended else None,
            "report": report[-2000:], "errors": errors, "denials": denials}


def scan_runs(projects, since):
    """Scheduled runs (not the control tower's own) whose transcript changed after `since`."""
    runs = []
    for path in sorted(Path(projects).glob("*/*.jsonl")):
        if path.stat().st_mtime < since.timestamp():
            continue
        run = read_run(path)
        if run and run["task"] != SELF and run["ended"] and parse_ts(run["ended"]) > since:
            runs.append(run)
    return runs


def score_run(run):
    score, reasons = 0, []
    if run["denials"]:
        score += 3
        reasons.append(f"{run['denials']} tool call(s) denied")
    if run["errors"] >= NOISY_ERRORS:
        score += 1
        reasons.append(f"{run['errors']} tool errors")
    if not run["report"].strip():
        score += 3
        reasons.append("no final report (run may have died)")
    else:
        m = TROUBLE_RE.search(run["report"])
        if m:
            score += 2
            reasons.append(f"report says '{m.group(0)}'")
    return score, reasons


# --- cron slots ------------------------------------------------------------------------------
# The Claude desktop app keeps its scheduled tasks in a JSON store. Path differs per install;
# set FLEET_TASKS_FILE to point at yours, or the newest match of this glob is used.
APP_STORE_GLOB = "Library/Application Support/Claude/claude-code-sessions/*/*/scheduled-tasks.json"
RETIME_WINDOW = timedelta(hours=2)   # a run scheduled this long before a slot covers it
SLOT_GRACE = timedelta(minutes=45)   # the app adds up to ~13 min of jitter; runs can queue
SLOT_LOOKBACK = timedelta(days=8)


def _cron_field(spec, lo, hi):
    vals = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step = part.split("/")
            step = int(step)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = map(int, part.split("-"))
        else:
            a = b = int(part)
        vals.update(range(a, b + 1, step))
    return vals


def _cron_sets(expr):
    m, h, dom, mon, dow = expr.split()
    dows = _cron_field(dow, 0, 7)
    if 7 in dows:
        dows.add(0)
    return (_cron_field(m, 0, 59), _cron_field(h, 0, 23), _cron_field(dom, 1, 31),
            _cron_field(mon, 1, 12), dows)


def cron_matches(expr, dt, _sets=None):
    """dt in LOCAL time (the app evaluates cron in local time). Sunday = 0 or 7."""
    m, h, dom, mon, dow = _sets or _cron_sets(expr)
    return (dt.minute in m and dt.hour in h and dt.day in dom and dt.month in mon
            and (dt.weekday() + 1) % 7 in dow)


def last_fire(expr, before, lookback=SLOT_LOOKBACK):
    """Latest cron slot at or before `before` (local time), or None within the lookback."""
    sets = _cron_sets(expr)
    t = before.astimezone().replace(second=0, microsecond=0)
    floor = t - lookback
    while t >= floor:
        if cron_matches(expr, t, sets):
            return t
        t -= timedelta(minutes=1)
    return None


def app_tasks(home=HOME):
    """The Claude app's own scheduled-task records (cron, enabled, lastRunAt)."""
    override = os.environ.get("FLEET_TASKS_FILE")
    if override:
        return load_json(Path(override), {})
    stores = sorted(home.glob(APP_STORE_GLOB), key=lambda p: p.stat().st_mtime)
    return load_json(stores[-1], {}) if stores else {}


def missed_slots(tasks, now):
    """Enabled cron tasks whose latest due slot (older than SLOT_GRACE) has no run after it."""
    events = []
    for t in tasks:
        if not t.get("enabled") or not t.get("cronExpression") or t.get("id") == SELF:
            continue
        slot = last_fire(t["cronExpression"], now - SLOT_GRACE)
        if slot is None:
            continue
        # Task created after its latest slot: nothing could have run yet.
        if t.get("createdAt") and datetime.fromtimestamp(t["createdAt"] / 1000, timezone.utc) > slot:
            continue
        last = parse_ts(t["lastRunAt"]) if t.get("lastRunAt") else None
        sched = parse_ts(t["lastScheduledFor"]) if t.get("lastScheduledFor") else None
        # Cron retimed after the run: the slot's run fired at the OLD time, the same day,
        # shortly before the new slot. Not a miss.
        if (last and sched and last >= sched - timedelta(minutes=1)
                and slot - RETIME_WINDOW <= sched <= slot):
            continue
        if last is None or last < slot - timedelta(minutes=1):
            ran = f"last run {last.astimezone():%d %b %H:%M}" if last else "never ran"
            events.append({"kind": "missed", "task": t["id"], "area": area_of(t["id"]), "score": 5,
                           "reasons": [f"{slot:%a %H:%M} slot did not start ({ran})"]})
    return events


def read_alerts(path, since):
    """Alerts agents appended since the last run (one JSON object per line)."""
    events = []
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return events
    for line in lines:
        try:
            a = json.loads(line)
            ts = parse_ts(a["ts"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
        if ts.tzinfo is None:
            ts = ts.astimezone()
        if ts <= since:
            continue
        events.append({"kind": "alert", "task": a.get("agent"), "area": area_of(a.get("agent")),
                       "score": ALERT_SCORE.get(a.get("severity"), 3), "severity": a.get("severity"),
                       "reasons": [a.get("msg", "")], "item": a.get("item"), "ended": ts.isoformat()})
    return events


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--projects", default=PROJECTS, type=Path)
    ap.add_argument("--state-dir", default=STATE_DIR, type=Path)
    ap.add_argument("--alerts", default=None, type=Path)
    ap.add_argument("--now", type=parse_ts, default=None)
    args = ap.parse_args(argv)
    alerts = args.alerts or args.state_dir / "alerts.jsonl"

    now = args.now or datetime.now(timezone.utc)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    state = load_json(args.state_dir / "state.json", {})
    since = parse_ts(state["last_run"]) if state.get("last_run") else now - FIRST_LOOKBACK
    if state.get("last_run") and timedelta(0) <= now - since < OVERLAP:
        print(f"OVERLAP - another control-tower run started {(now - since).total_seconds():.0f}s ago. "
              "Stop this run: write nothing, send nothing.")
        return EXIT_OVERLAP

    events = []
    last_seen = {t: parse_ts(v) for t, v in state.get("last_seen", {}).items()}
    for run in scan_runs(args.projects, since):
        ended = parse_ts(run["ended"])
        if run["task"] not in last_seen or ended > last_seen[run["task"]]:
            last_seen[run["task"]] = ended
        score, reasons = score_run(run)
        events.append({"kind": "run", "task": run["task"], "area": area_of(run["task"]),
                       "score": score, "reasons": reasons, "session": run["session"],
                       "ended": run["ended"], "report": run["report"]})
    store = app_tasks()
    if store.get("scheduledTasks"):
        events += missed_slots(store["scheduledTasks"], now)
    events += read_alerts(alerts, since)

    events.sort(key=lambda e: -e["score"])
    out = {"run_started": now.isoformat(), "since": since.isoformat(), "threshold": THRESHOLD,
           "quiet": not any(e["score"] >= THRESHOLD for e in events),
           "summary": {"runs": sum(e["kind"] == "run" for e in events),
                       "runs_ok": sum(e["kind"] == "run" and e["score"] < THRESHOLD for e in events),
                       "hot": sum(e["score"] >= THRESHOLD for e in events)},
           "events": events}
    (args.state_dir / "events.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    state.update(last_run=now.isoformat(), last_seen={t: v.isoformat() for t, v in last_seen.items()})
    (args.state_dir / "state.json").write_text(json.dumps(state, indent=2))

    hot = [e for e in events if e["score"] >= THRESHOLD]
    print(f"{len(events)} events, {len(hot)} at/above {THRESHOLD}" + (" - QUIET" if out["quiet"] else ""))
    for e in hot:
        print(f"  [{e['score']}] {e['kind']:8} {e['task'] or '-':28} {'; '.join(e['reasons'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
