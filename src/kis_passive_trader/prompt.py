"""
Cross-platform stdin reads with a timeout.

`select` on stdin doesn't work on Windows, so we use a single background
daemon thread that reads lines from stdin and feeds them to a queue. Each
prompt then waits on the queue with a timeout. Using *one* persistent reader
(rather than one thread per prompt) avoids multiple threads racing to read
the same stdin after a prompt times out.
"""

from __future__ import annotations

import queue
import sys
import threading
from typing import TextIO


class TimedInput:
    """Read lines from a stream with a per-prompt timeout."""

    def __init__(self, stream: TextIO | None = None):
        self._stream = stream if stream is not None else sys.stdin
        self._q: "queue.Queue[str]" = queue.Queue()
        self._started = False
        self._lock = threading.Lock()

    def _reader(self) -> None:
        # Iterating the stream yields one line at a time and ends at EOF.
        try:
            for line in self._stream:
                self._q.put(line.rstrip("\n"))
        except (ValueError, OSError):
            # Stream closed underneath us — nothing more will arrive.
            pass

    def _ensure_started(self) -> None:
        with self._lock:
            if not self._started:
                threading.Thread(target=self._reader, daemon=True).start()
                self._started = True

    def _drain(self) -> None:
        """Discard anything typed before this prompt was shown."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def prompt(self, message: str, timeout: float) -> str | None:
        """Show `message`, then return one line (stripped), or None on timeout."""
        self._ensure_started()
        self._drain()
        print(message, end="", flush=True)
        try:
            return self._q.get(timeout=timeout).strip()
        except queue.Empty:
            print()   # finish the prompt line that went unanswered
            return None
