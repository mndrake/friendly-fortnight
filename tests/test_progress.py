"""Progress reporter unit tests — fake clock, throttling, rate/ETA math,
and the no-op default path."""
from __future__ import annotations

from lineage.extract.progress import NULL, Progress, _fmt_duration


class FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _prog(clock: FakeClock, min_interval: float = 1.0
          ) -> tuple[Progress, list[str]]:
    out: list[str] = []
    return Progress(echo=out.append, clock=clock, min_interval=min_interval), out


# --- formatting ---------------------------------------------------------------

def test_fmt_duration_mmss_and_hmmss():
    assert _fmt_duration(0) == "00:00"
    assert _fmt_duration(5) == "00:05"
    assert _fmt_duration(65) == "01:05"
    assert _fmt_duration(3599) == "59:59"
    assert _fmt_duration(3600) == "1:00:00"
    assert _fmt_duration(3661) == "1:01:01"
    assert _fmt_duration(-3) == "00:00"   # defensive: clock skew never crashes


# --- phases -------------------------------------------------------------------

def test_phase_transitions_print_done_with_elapsed():
    clock = FakeClock()
    p, out = _prog(clock)
    p.phase("seed pass")
    clock.advance(5)
    p.phase("round 1")
    clock.advance(65)
    p.close()
    assert out == [
        "== seed pass",
        "== seed pass done in 00:05",
        "== round 1",
        "== round 1 done in 01:05",
        "total elapsed 01:10",
    ]


def test_close_without_phase_prints_nothing_when_idle():
    clock = FakeClock()
    p, out = _prog(clock)
    p.close()
    assert out == []   # no output ever happened — nothing to summarize


# --- start / done -------------------------------------------------------------

def test_start_done_reports_elapsed_and_kwargs():
    clock = FakeClock()
    p, out = _prog(clock)
    p.start("DSPPGMREF APPLIB/*ALL")
    clock.advance(12)
    p.done("DSPPGMREF APPLIB/*ALL", rows=42, failures=1)
    assert out == [
        "  DSPPGMREF APPLIB/*ALL ...",
        "  DSPPGMREF APPLIB/*ALL: done in 00:12 (rows=42, failures=1)",
    ]


def test_done_without_start_is_safe():
    clock = FakeClock()
    p, out = _prog(clock)
    p.done("orphan", rows=0)
    assert out == ["  orphan: done in 00:00 (rows=0)"]


# --- tick throttling ----------------------------------------------------------

def test_tick_throttles_within_interval_with_total():
    clock = FakeClock()
    p, out = _prog(clock)
    for i in range(1, 101):
        p.tick("items", i, 100)     # clock frozen: within min_interval
    # First tick and the done==total tick always print; the rest suppress.
    assert out == ["  items: 1/100", "  items: 100/100"]


def test_tick_throttles_within_interval_without_total():
    clock = FakeClock()
    p, out = _prog(clock)
    for i in range(1, 101):
        p.tick("items", i)
    assert out == ["  items: 1"]    # no total -> no forced final print


def test_tick_prints_when_spaced_beyond_interval():
    clock = FakeClock()
    p, out = _prog(clock)
    for i in range(1, 5):
        p.tick("items", i, 100)
        clock.advance(1.5)
    assert len(out) == 4


# --- tick rate / ETA math -----------------------------------------------------

def test_tick_rate_and_eta_math():
    clock = FakeClock()
    p, out = _prog(clock)
    p.tick("items", 1, 100)          # baseline at t=0
    clock.advance(1.0)
    p.tick("items", 2, 100)          # 1 item / 1s since baseline
    assert out[-1] == "  items: 2/100 (1.0/s, ETA 01:38)"   # 98 left @ 1/s
    clock.advance(1.0)
    p.tick("items", 4, 100)          # 3 items / 2s = 1.5/s; 96 left -> 64s
    assert out[-1] == "  items: 4/100 (1.5/s, ETA 01:04)"


def test_tick_eta_switches_to_hmmss_above_an_hour():
    clock = FakeClock()
    p, out = _prog(clock)
    p.tick("items", 1, 10000)
    clock.advance(1.0)
    p.tick("items", 2, 10000)        # 1/s -> 9998s remaining
    assert out[-1] == "  items: 2/10000 (1.0/s, ETA 2:46:38)"


def test_tick_rate_only_without_total():
    clock = FakeClock()
    p, out = _prog(clock)
    p.tick("members", 1)
    clock.advance(2.0)
    p.tick("members", 5)             # 4 items / 2s = 2.0/s
    assert out[-1] == "  members: 5 (2.0/s)"


# --- no-op path ---------------------------------------------------------------

def test_echo_none_is_a_safe_noop():
    for p in (Progress(echo=None), NULL):
        p.phase("x")
        p.note("y")
        p.start("z")
        p.done("z", rows=1)
        p.tick("t", 1, 10)
        p.tick("t", 10, 10)
        p.close()   # no exception, no output channel to observe


def test_note_is_unthrottled():
    clock = FakeClock()
    p, out = _prog(clock)
    p.note("a")
    p.note("b")
    assert out == ["  a", "  b"]
