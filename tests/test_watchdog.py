"""The watchdog reports on Telegram when the app stops or freezes, and never starts it again."""

from __future__ import annotations

from core.watchdog import FROZEN_AFTER, Watchdog, read_heartbeat, write_heartbeat


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def make(tmp_path, alive=True):
    clock, sent, state = Clock(), [], {"alive": alive}
    beat = tmp_path / "heartbeat.json"
    dog = Watchdog(42, beat, lambda title, body: sent.append((title, body)), now=clock,
                   alive=lambda pid: state["alive"], free_mb=lambda: 900)
    return dog, clock, sent, state, beat


def test_a_crash_is_reported_once_and_the_watch_ends(tmp_path):
    dog, clock, sent, state, beat = make(tmp_path)
    write_heartbeat(beat, 42, clock.t - 3 * 3600, now=clock.t)
    assert dog.check() and sent == []
    clock.t += 60
    state["alive"] = False
    assert dog.check() is False
    assert len(sent) == 1 and sent[0][0] == "Auto Apply Studio stopped"
    assert "3 h 01 min" in sent[0][1] and "900 MB" in sent[0][1] and "will not start again" in sent[0][1]


def test_closing_from_the_window_is_told_apart_from_a_crash(tmp_path):
    dog, clock, sent, state, beat = make(tmp_path)
    write_heartbeat(beat, 42, clock.t - 600, closed=True, now=clock.t)
    state["alive"] = False
    assert dog.check() is False
    assert sent[0][0] == "Auto Apply Studio was closed"


def test_a_frozen_window_is_reported_once_and_its_recovery_too(tmp_path):
    dog, clock, sent, state, beat = make(tmp_path)
    write_heartbeat(beat, 42, clock.t, now=clock.t)
    clock.t += FROZEN_AFTER + 30
    assert dog.check() and dog.check()
    assert [t for t, _ in sent] == ["Auto Apply Studio is not responding"]
    write_heartbeat(beat, 42, clock.t - 4000, now=clock.t)
    assert dog.check()
    assert sent[-1][0] == "Auto Apply Studio is responding again"


def test_a_heartbeat_from_an_earlier_run_is_not_ours(tmp_path):
    dog, clock, sent, state, beat = make(tmp_path)
    write_heartbeat(beat, 7, clock.t - 100, closed=True, now=clock.t - 9999)
    assert dog.check() and sent == []  # not frozen: that heartbeat belongs to another process
    state["alive"] = False
    dog.check()
    assert sent[0][0] == "Auto Apply Studio stopped"  # its "closed" mark is not ours either
    assert read_heartbeat(tmp_path / "missing.json") == {}
