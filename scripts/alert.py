#!/usr/bin/env python3
"""Append one alert line to ~/.claude/agent-manager/alerts.jsonl (read by gate.py).

An agent that fails or skips silently is the problem this solves: every infrastructure failure
or skip should leave a line.

  alert.py <agent> <failed|needs-human|info> "<msg, plain, <=140 chars>" [--item ID] [--once KEY]

--once KEY: write only if no alert with this key was written in the last 24 h (first occurrence,
not every run).
Line format: {"ts","agent","severity","msg","item"} (+ "key" when --once is used).
Prints "written" or "dup". Also importable: from alert import alert
"""
import datetime as dt
import json
import os
import sys

PATH = os.path.expanduser(os.environ.get("ALERTS_FILE", "~/.claude/agent-manager/alerts.jsonl"))
SEVERITIES = ("failed", "needs-human", "info")


def alert(agent, severity, msg, item=None, once=None, path=None, now=None):
    path = path or PATH
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}")
    now = now or dt.datetime.now().astimezone()
    msg = " ".join(str(msg).split())[:140]
    if once:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        a = json.loads(line)
                        if a.get("key") == once and now - dt.datetime.fromisoformat(a["ts"]) < dt.timedelta(hours=24):
                            return False
                    except (ValueError, KeyError, TypeError):
                        continue
        except FileNotFoundError:
            pass
    row = {"ts": now.isoformat(timespec="seconds"), "agent": agent, "severity": severity, "msg": msg, "item": item}
    if once:
        row["key"] = once
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


if __name__ == "__main__":
    a = sys.argv[1:]
    opts = {}
    for flag in ("--item", "--once"):
        if flag in a:
            i = a.index(flag)
            opts[flag[2:]] = a[i + 1]
            del a[i:i + 2]
    if len(a) < 3:
        print(__doc__)
        sys.exit(2)
    print("written" if alert(a[0], a[1], " ".join(a[2:]), item=opts.get("item"), once=opts.get("once")) else "dup")
