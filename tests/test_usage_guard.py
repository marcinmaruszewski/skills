"""Tests for the --gate policy of skills/usage-guard/scripts/usage_guard.py.

Run from the repository root: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "skills/usage-guard/scripts/usage_guard.py"
spec = importlib.util.spec_from_file_location("usage_guard", SCRIPT)
ug = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ug  # dataclasses look their module up here
spec.loader.exec_module(ug)

CLAUDE = ug.PROVIDERS[0]
NOW = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)


def usage(five_hour: float, seven_day: float = 50.0, five_hour_reset: timedelta = timedelta(hours=3)) -> ug.Result:
    return ug.Result(
        [
            ug.Window("5h", five_hour, NOW + five_hour_reset),
            ug.Window("7d", seven_day, NOW + timedelta(days=2)),
        ]
    )


class IsolatedCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        # Keep pause timestamps in a throwaway cache directory.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(ug.tempfile, "tempdir", tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)


class GateTest(IsolatedCacheTest):
    def test_continue_below_threshold(self) -> None:
        decision = ug.gate(CLAUDE, usage(90.0), None, NOW)
        self.assertEqual(decision.code, ug.EXIT_CONTINUE)
        self.assertEqual(decision.line, "continue: 5h 90.0% used, 7d 50.0% used")

    def test_pause_at_threshold_until_reset(self) -> None:
        decision = ug.gate(CLAUDE, usage(95.0, five_hour_reset=timedelta(minutes=4)), None, NOW)
        self.assertEqual(decision.code, ug.EXIT_PAUSE)
        self.assertEqual(decision.sleep, 4 * 60 + 60)
        self.assertTrue(decision.line.startswith("pause: 5h 95.0% used, resets "))
        self.assertTrue(decision.line.endswith("; sleep 300"))

    def test_pause_sleep_is_capped(self) -> None:
        decision = ug.gate(CLAUDE, usage(99.0), None, NOW)
        self.assertEqual(decision.sleep, ug.MAX_SLEEP)

    def test_stop_when_reset_is_beyond_max_wait(self) -> None:
        decision = ug.gate(CLAUDE, usage(10.0, seven_day=96.0), None, NOW)
        self.assertEqual(decision.code, ug.EXIT_STOP)
        self.assertTrue(decision.line.startswith("stop: 7d 96.0% used, resets "))

    def test_stop_after_pausing_for_max_wait(self) -> None:
        started = time.time() - (ug.MAX_WAIT_MINUTES + 1) * 60
        cache = ug._cache_dir()
        (cache / "claude-pause.json").write_text(json.dumps({"started": started, "last": time.time()}))
        decision = ug.gate(CLAUDE, usage(99.0), None, NOW)
        self.assertEqual(decision.code, ug.EXIT_STOP)
        self.assertTrue(decision.line.endswith("; already waited 6 h"))

    def test_error_passes_through(self) -> None:
        decision = ug.gate(CLAUDE, ug.Result([], error="HTTP 401"), None, NOW)
        self.assertEqual(decision, ug.Decision(ug.EXIT_ERROR, "error: HTTP 401"))


class ReserveTest(IsolatedCacheTest):
    def test_reserve_pauses_when_the_unit_would_overrun(self) -> None:
        decision = ug.gate(CLAUDE, usage(90.0), None, NOW, reserve=10)
        self.assertEqual(decision.code, ug.EXIT_PAUSE)
        self.assertTrue(decision.line.startswith("pause: 5h 90.0% used + 10% reserved, resets "))

    def test_reserve_continues_when_the_unit_fits(self) -> None:
        decision = ug.gate(CLAUDE, usage(80.0), None, NOW, reserve=10)
        self.assertEqual(decision.code, ug.EXIT_CONTINUE)

    def test_reserve_leaves_the_weekly_window_alone(self) -> None:
        decision = ug.gate(CLAUDE, usage(10.0, seven_day=90.0), None, NOW, reserve=10)
        self.assertEqual(decision.code, ug.EXIT_CONTINUE)

    def test_exhausted_window_reports_without_reserve(self) -> None:
        decision = ug.gate(CLAUDE, usage(96.0), None, NOW, reserve=10)
        self.assertTrue(decision.line.startswith("pause: 5h 96.0% used, resets "))


class WaitTest(IsolatedCacheTest):
    def run_wait(self, results: list[ug.Result], reserve: float = 0.0) -> tuple[ug.Decision, list[float]]:
        sleeps: list[float] = []
        with mock.patch.object(ug, "collect", side_effect=results), mock.patch.object(
            ug.time, "sleep", side_effect=sleeps.append
        ), mock.patch.object(ug, "datetime", wraps=datetime) as fake_datetime:
            fake_datetime.now.return_value = NOW
            decision = ug.wait_gate(CLAUDE, None, reserve)
        return decision, sleeps

    def test_returns_at_once_on_continue(self) -> None:
        decision, sleeps = self.run_wait([usage(10.0)])
        self.assertEqual(decision.code, ug.EXIT_CONTINUE)
        self.assertEqual(sleeps, [])

    def test_sleeps_through_pause_and_errors_until_continue(self) -> None:
        results = [usage(99.0), ug.Result([], error="network error"), usage(0.0)]
        decision, sleeps = self.run_wait(results)
        self.assertEqual(decision.code, ug.EXIT_CONTINUE)
        self.assertEqual(sleeps, [ug.WAIT_POLL, ug.WAIT_POLL])

    def test_sleeps_until_shortly_after_reset(self) -> None:
        results = [usage(99.0, five_hour_reset=timedelta(minutes=1)), usage(0.0)]
        _, sleeps = self.run_wait(results)
        self.assertEqual(sleeps, [120])

    def test_keeps_the_reserve_while_waiting(self) -> None:
        decision, sleeps = self.run_wait([usage(90.0), usage(0.0)], reserve=10)
        self.assertEqual(decision.code, ug.EXIT_CONTINUE)
        self.assertEqual(len(sleeps), 1)

    def test_returns_stop(self) -> None:
        decision, sleeps = self.run_wait([usage(10.0, seven_day=96.0)])
        self.assertEqual(decision.code, ug.EXIT_STOP)
        self.assertEqual(sleeps, [])

    def test_gives_up_on_errors_after_max_wait(self) -> None:
        start = time.time()
        clock = iter([start, start + 60, start + ug.MAX_WAIT_MINUTES * 60])
        failing = ug.Result([], error="network error")
        with mock.patch.object(ug.time, "time", side_effect=lambda: next(clock)):
            decision, sleeps = self.run_wait([failing, failing])
        self.assertEqual(decision.code, ug.EXIT_ERROR)
        self.assertEqual(sleeps, [ug.WAIT_POLL])


    def test_pause_after_failed_checks_stops_at_the_deadline(self) -> None:
        start = time.time()
        past_deadline = start + ug.MAX_WAIT_MINUTES * 60 + 5
        clock = iter([start, start + 1, past_deadline, past_deadline + 1, past_deadline + 2])
        failing = ug.Result([], error="network error")
        with mock.patch.object(ug.time, "time", side_effect=lambda: next(clock)):
            decision, sleeps = self.run_wait([failing, usage(99.0)])
        self.assertEqual(decision.code, ug.EXIT_STOP)
        self.assertTrue(decision.line.startswith("stop: 5h 99.0% used, resets "))
        self.assertTrue(decision.line.endswith("; already waited 6 h"))
        self.assertEqual(len(sleeps), 1)
        self.assertFalse((ug._cache_dir() / "claude-pause.json").exists())


class ArgsTest(unittest.TestCase):
    def assert_rejected(self, *argv: str) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ug.parse_args(list(argv))

    def test_reserve_and_wait_need_gate(self) -> None:
        self.assert_rejected("claude", "--reserve", "10")
        self.assert_rejected("claude", "--wait")

    def test_reserve_range(self) -> None:
        self.assert_rejected("claude", "--gate", "--reserve", "-1")
        self.assert_rejected("claude", "--gate", "--reserve", "95")

    def test_accepts_reserve_with_wait(self) -> None:
        args = ug.parse_args(["claude", "--gate", "--reserve", "12.5", "--wait"])
        self.assertEqual((args.reserve, args.wait), (12.5, True))


if __name__ == "__main__":
    unittest.main()
