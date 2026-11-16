#!/usr/bin/env python3
"""Dead-man check over pings.jsonl. Run it every 5 minutes (cron or launchd, see the README).

A run that pinged `start` but never `ok`/`fail` is flagged when either
  - it has been open longer than its limit (default 90 min, override per task in limits.json): hung or crashed, or
  - the same task started again since: the earlier run died without finishing.
Each unfinished run is alerted once (alerts.jsonl, severity failed, key deadman-<task>-<start ts>)
and shown as a macOS notification, so you hear about it within minutes, not at the next report.

  deadman.py [--dry] [--now ISO] [--pings FILE]

limits.json (optional, next to this script or in ~/.claude/agent-manager/): {"seo-weekly": 180, "poster": 45}
"""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alert import alert  # noqa: E402

PINGS = Path(os.path.expanduser(os.environ.get("PINGS_FILE", "~/.claude/agent-manager/pings.jsonl")))
LIMITS_FILE = Path(os.path.expanduser("~/.claude/agent-manager/limits.json"))
DEFAULT_LIMIT = 90
LOOKBACK = dt.timedelta(hours=36)
DUP_START = dt.timedelta(minutes=5)


def load_limits(path=LIMITS_FILE):
    try:
        return {k: float(v) for k, v in json.loads(Path(path).read_text()).items()}
    except (FileNotFoundError, ValueError, TypeError):
        return {}


def unfinished(rows, now, limits=None):
    """Return [(task, start_ts, reason)] for runs that started and never finished."""
    limits = limits or {}
    open_runs, out = {}, []
    for r in rows:
        task, ev = r.get("task"), r.get("event")
        try:
            ts = dt.datetime.fromisoformat(r["ts"])
        except (KeyError, ValueError, TypeError):
            continue
        if ev == "start":
            if task in open_runs and (ts - open_runs[task]) < DUP_START:
                continue  # duplicate start ping from the same run
            if task in open_runs and ts - open_runs[task] < dt.timedelta(minutes=10):
                pass  # same run pinged start twice (retry inside the run), not a dead run
            elif task in open_runs:
                out.append((task, open_runs[task], f"started {open_runs[task]:%H:%M}, never finished (it ran again at {ts:%H:%M})"))
            open_runs[task] = ts
        elif ev in ("ok", "fail"):
            open_runs.pop(task, None)
    for task, ts in open_runs.items():
        age = (now - ts).total_seconds() / 60
        if age > limits.get(task, DEFAULT_LIMIT):
            out.append((task, ts, f"started {ts:%H:%M}, still not finished after {age:.0f} min (hung or crashed)"))
    return [(t, s, why) for t, s, why in out if now - s < LOOKBACK]


def read_rows(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return rows


def notify(msg):
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{msg[:120]}" with title "Agent did not finish"'],
                       capture_output=True)


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    now = dt.datetime.fromisoformat(a[a.index("--now") + 1]) if "--now" in a else dt.datetime.now().astimezone()
    dry = "--dry" in a
    path = Path(a[a.index("--pings") + 1]) if "--pings" in a else PINGS
    hits = unfinished(read_rows(path), now, load_limits())
    for task, start, why in hits:
        msg = f"{task} {why}"
        if dry:
            print("WOULD ALERT", msg)
            continue
        if alert("deadman", "failed", msg, once=f"deadman-{task}-{start.isoformat(timespec='minutes')}"):
            notify(msg)
            print("ALERT", msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
