"""Live progress feedback for long-running extractions.

A targeted extract against a real estate runs for many minutes across
hundreds of small host calls (per-file DSPFFD/DSPDBR, objstat probes, member
retrievals). With no output until the final counts, an operator cannot tell
a healthy slow run from a hung one. :class:`Progress` gives the extract
layer a single, dependency-free channel for phase markers, per-step timings,
and throttled item counters with rate/ETA — written wherever the caller's
``echo`` points (the CLI sends it to stderr so the stdout count summary
stays clean and scriptable).

Every method is a no-op when ``echo`` is None, so library callers default to
the shared :data:`NULL` instance and pay nothing; only the CLI constructs a
live one. Timing uses an injectable ``clock`` (default ``time.monotonic``)
so tests drive it deterministically.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional


def _fmt_duration(seconds: float) -> str:
    """``mm:ss`` under an hour, ``h:mm:ss`` above."""
    total = max(int(round(seconds)), 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class Progress:
    """Phase/step/counter reporting with per-label throttling.

    * ``phase(name)`` — section marker; closes the previous phase with its
      elapsed time.
    * ``start(label)`` / ``done(label, **info)`` — brackets one blocking
      step (a broad DSP command, a catalog pull); ``done`` reports elapsed
      plus any ``key=value`` info.
    * ``tick(label, done, total=None)`` — item counter for loops. Prints at
      most once per ``min_interval`` seconds per label (first tick and the
      ``done == total`` tick always print). Rate is measured against the
      label's *first* tick, so ETA stays honest across the whole loop.
    * ``note(msg)`` — unthrottled one-off line (warnings, round summaries).
    * ``close()`` — closes the open phase and prints total elapsed.
    """

    def __init__(self, echo: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 min_interval: float = 1.0,
                 stamp: Optional[Callable[[], str]] = None):
        self.echo = echo
        self.clock = clock
        self.min_interval = min_interval
        # Optional wall-clock stamp prefixed to every line ("[HH:MM:SS] ").
        # The CLI passes one so progress lines correlate with the
        # timestamped host-call log when troubleshooting a long run.
        self.stamp = stamp
        self._run_start: Optional[float] = None
        self._phase: Optional[str] = None
        self._phase_start: float = 0.0
        self._starts: dict[str, float] = {}
        # label -> (first_ts, first_done, last_print_ts)
        self._ticks: dict[str, tuple[float, int, float]] = {}

    def _emit(self, line: str) -> None:
        if self.stamp is not None:
            line = f"[{self.stamp()}] {line}"
        self.echo(line)

    def _now(self) -> float:
        now = self.clock()
        if self._run_start is None:
            self._run_start = now
        return now

    def phase(self, name: str) -> None:
        if self.echo is None:
            return
        now = self._now()
        self._close_phase(now)
        self._phase = name
        self._phase_start = now
        self._emit(f"== {name}")

    def note(self, msg: str) -> None:
        if self.echo is None:
            return
        self._now()
        self._emit(f"  {msg}")

    def start(self, label: str) -> None:
        if self.echo is None:
            return
        self._starts[label] = self._now()
        self._emit(f"  {label} ...")

    def done(self, label: str, **info: Any) -> None:
        if self.echo is None:
            return
        now = self._now()
        elapsed = now - self._starts.pop(label, now)
        suffix = ""
        if info:
            suffix = " (" + ", ".join(f"{k}={v}" for k, v in info.items()) + ")"
        self._emit(f"  {label}: done in {_fmt_duration(elapsed)}{suffix}")

    def tick(self, label: str, done: int, total: Optional[int] = None) -> None:
        if self.echo is None:
            return
        now = self._now()
        state = self._ticks.get(label)
        if state is None:
            # First tick is the rate baseline: always printed, never rated
            # (zero elapsed would make any rate meaningless).
            self._ticks[label] = (now, done, now)
            self._emit(f"  {label}: {done}/{total}" if total is not None
                      else f"  {label}: {done}")
            return
        first_ts, first_done, last_print = state
        final = total is not None and done >= total
        if not final and now - last_print < self.min_interval:
            return
        elapsed = now - first_ts
        rate = (done - first_done) / elapsed if elapsed > 0 else 0.0
        if total is not None:
            if rate > 0:
                eta = _fmt_duration(max(total - done, 0) / rate)
                line = f"  {label}: {done}/{total} ({rate:.1f}/s, ETA {eta})"
            else:
                line = f"  {label}: {done}/{total}"
        else:
            if rate > 0:
                line = f"  {label}: {done} ({rate:.1f}/s)"
            else:
                line = f"  {label}: {done}"
        self._ticks[label] = (first_ts, first_done, now)
        self._emit(line)

    def close(self) -> None:
        if self.echo is None:
            return
        now = self.clock()
        self._close_phase(now)
        if self._run_start is not None:
            self._emit(f"total elapsed {_fmt_duration(now - self._run_start)}")

    def _close_phase(self, now: float) -> None:
        if self._phase is not None:
            self._emit(f"== {self._phase} done in "
                      f"{_fmt_duration(now - self._phase_start)}")
            self._phase = None


#: Shared no-op instance — the default for every ``progress=`` parameter in
#: the extract layer (``p = progress or NULL``).
NULL = Progress(echo=None)
