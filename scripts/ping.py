#!/usr/bin/env python3
"""Dead-man pings for scheduled agents: alarm when a run starts and never finishes.

  ping.py <task> start [--max-late MIN]   first step of a run
  ping.py <task> ok    ["note"]           last step of a successful run
  ping.py <task> fail  ["note"]           last step when the run gave up

Appends {"ts","task","event","note"} to ~/.claude/agent-manager/pings.jsonl.
start --max-late N: if this run started more than N minutes after its cron slot (laptop asleep,
scheduler stuck, catch-up after wake) it prints LATE, logs event "late-skip" and exits 3. The agent
must then stop without doing its work and without an ok ping. Without --max-late, start always exits 0.
deadman.py (run every 5 minutes by cron or launchd) alerts on a start with no ok/fail.
"""
import datetime as dt
import json
import os
import sys
from pathlib import Path

PINGS = Path(os.path.expanduser(os.environ.get("PINGS_FILE", "~/.claude/agent-manager/pings.jsonl")))
EXIT_LATE = 3


def write(task, event, note="", now=None, path=None):
    path = path or PINGS
    now = now or dt.datetime.now().astimezone()
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": now.isoformat(timespec="seconds"), "task": task, "event": event, "note": " ".join(str(note).split())[:200]}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def lateness_min(task, now, tasks=None):
    """Minutes since the task's latest cron slot, or None if unknown (no cron / not found)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gate
    if tasks is None:
        tasks = gate.app_tasks().get("scheduledTasks", [])
    t = next((t for t in tasks if t.get("id") == task), None)
    if not t or not t.get("cronExpression"):
        return None
    slot = gate.last_fire(t["cronExpression"], now)
    return None if slot is None else (now - slot).total_seconds() / 60


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) < 2 or a[1] not in ("start", "ok", "fail"):
        print(__doc__)
        return 2
    task, event = a[0], a[1]
    now = dt.datetime.now().astimezone()
    if event == "start" and "--max-late" in a:
        limit = float(a[a.index("--max-late") + 1])
        late = lateness_min(task, now)
        if late is not None and late > limit:
            write(task, "late-skip", f"{late:.0f} min after slot")
            print(f"LATE {late:.0f} min after its slot (limit {limit:.0f}): skip this run, no ok ping")
            return EXIT_LATE
    note = " ".join(a[2:]) if event != "start" else ""
    write(task, event, note)
    print(event)
    return 0


if __name__ == "__main__":
    sys.exit(main())
