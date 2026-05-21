"""Tests for the timed-input helper (no real stdin)."""

from __future__ import annotations

import threading
import time

from kis_passive_trader.prompt import TimedInput


def _no_reader() -> TimedInput:
    """A TimedInput with the background stdin reader suppressed, so tests can
    drive the internal queue directly and deterministically."""
    ti = TimedInput()
    ti._started = True
    return ti


def test_prompt_returns_input():
    ti = _no_reader()

    def feed():
        time.sleep(0.05)
        ti._q.put(" y ")

    threading.Thread(target=feed, daemon=True).start()
    assert ti.prompt("? ", timeout=2.0) == "y"   # stripped


def test_prompt_times_out():
    ti = _no_reader()
    assert ti.prompt("? ", timeout=0.1) is None


def test_prompt_drains_stale_input():
    ti = _no_reader()
    ti._q.put("stale")          # typed before the prompt was shown
    assert ti.prompt("? ", timeout=0.1) is None


class _DelayedStream:
    """A stream that yields one line after a short delay, mimicking a human
    typing *after* the prompt is shown (so it survives the pre-prompt drain)."""
    def __init__(self, line: str, delay: float):
        self._line = line
        self._delay = delay
        self._sent = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._sent:
            raise StopIteration
        time.sleep(self._delay)
        self._sent = True
        return self._line


def test_real_reader_reads_a_line():
    # Exercises the real background reader thread + queue handoff.
    ti = TimedInput(stream=_DelayedStream("hello\n", delay=0.1))
    assert ti.prompt("? ", timeout=2.0) == "hello"
