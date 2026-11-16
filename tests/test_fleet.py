import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import alert as alert_mod  # noqa: E402
import deadman  # noqa: E402
import gate  # noqa: E402

TZ = dt.timezone.utc


def t(h, m=0, d=15):
    return dt.datetime(2026, 11, d, h, m, tzinfo=TZ)


def row(task, event, when):
    return {"ts": when.isoformat(), "task": task, "event": event}


class Cron(unittest.TestCase):
    def test_every_two_hours(self):
        self.assertTrue(gate.cron_matches("7 */2 * * *", dt.datetime(2026, 11, 15, 10, 7)))
        self.assertFalse(gate.cron_matches("7 */2 * * *", dt.datetime(2026, 11, 15, 11, 7)))

    def test_weekday_list_sunday_is_zero_or_seven(self):
        sunday = dt.datetime(2026, 11, 15, 9, 0)  # a Sunday
        self.assertTrue(gate.cron_matches("0 9 * * 0", sunday))
        self.assertTrue(gate.cron_matches("0 9 * * 7", sunday))
        self.assertFalse(gate.cron_matches("0 9 * * 1,3,5", sunday))

    def test_last_fire_finds_latest_slot(self):
        slot = gate.last_fire("37 */4 * * *", dt.datetime(2026, 11, 15, 14, 0).astimezone())
        self.assertEqual((slot.hour, slot.minute), (12, 37))


class Missed(unittest.TestCase):
    def task(self, **kw):
        base = {"id": "seo-weekly", "enabled": True, "cronExpression": "0 9 * * *"}
        base.update(kw)
        return base

    def test_never_ran_is_missed(self):
        now = dt.datetime(2026, 11, 15, 12, 0).astimezone()
        ev = gate.missed_slots([self.task()], now)
        self.assertEqual(len(ev), 1)
        self.assertIn("never ran", ev[0]["reasons"][0])

    def test_ran_after_slot_is_fine(self):
        now = dt.datetime(2026, 11, 15, 12, 0).astimezone()
        ran = dt.datetime(2026, 11, 15, 9, 3).astimezone().isoformat()
        self.assertEqual(gate.missed_slots([self.task(lastRunAt=ran)], now), [])

    def test_disabled_and_self_are_ignored(self):
        now = dt.datetime(2026, 11, 15, 12, 0).astimezone()
        self.assertEqual(gate.missed_slots([self.task(enabled=False), self.task(id=gate.SELF)], now), [])


class Deadman(unittest.TestCase):
    def test_finished_run_is_quiet(self):
        rows = [row("a", "start", t(9)), row("a", "ok", t(9, 20))]
        self.assertEqual(deadman.unfinished(rows, t(12)), [])

    def test_hung_run_is_flagged_after_limit(self):
        rows = [row("a", "start", t(9))]
        self.assertEqual(deadman.unfinished(rows, t(9, 30)), [])
        self.assertEqual(len(deadman.unfinished(rows, t(11))), 1)

    def test_per_task_limit(self):
        rows = [row("poster", "start", t(9))]
        self.assertEqual(len(deadman.unfinished(rows, t(10), {"poster": 45})), 1)

    def test_restart_means_previous_run_died(self):
        rows = [row("a", "start", t(9)), row("a", "start", t(13))]
        out = deadman.unfinished(rows, t(13, 5))
        self.assertEqual(len(out), 1)
        self.assertIn("never finished", out[0][2])

    def test_duplicate_start_ping_is_not_a_dead_run(self):
        rows = [row("a", "start", t(9)), row("a", "start", t(9, 0).replace(second=7)), row("a", "ok", t(9, 30))]
        self.assertEqual(deadman.unfinished(rows, t(12)), [])


class Alert(unittest.TestCase):
    def test_once_key_dedupes_within_24h(self):
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "alerts.jsonl")
            self.assertTrue(alert_mod.alert("x", "failed", "boom", once="k", path=p))
            self.assertFalse(alert_mod.alert("x", "failed", "boom", once="k", path=p))
            self.assertEqual(len(Path(p).read_text().splitlines()), 1)

    def test_bad_severity_rejected(self):
        with self.assertRaises(ValueError):
            alert_mod.alert("x", "loud", "boom", path="/dev/null")


class ScoreRun(unittest.TestCase):
    def test_empty_report_scores_hot(self):
        score, _ = gate.score_run({"denials": 0, "errors": 0, "report": ""})
        self.assertGreaterEqual(score, gate.THRESHOLD)

    def test_clean_report_is_quiet(self):
        score, _ = gate.score_run({"denials": 0, "errors": 2, "report": "Published 2 posts."})
        self.assertLess(score, gate.THRESHOLD)

    def test_delivery_failure_in_report(self):
        score, reasons = gate.score_run({"denials": 0, "errors": 0, "report": "WhatsApp was logged out, nothing sent."})
        self.assertGreater(score, 0)


if __name__ == "__main__":
    unittest.main()
