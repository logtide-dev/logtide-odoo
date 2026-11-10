import os

import pytest


class FakeClient:
    def __init__(self):
        self._closed = False
        self._buffer = ["parent-entry"]
        self._flush_timer = None
        self.flushed = 0
        self.closed_calls = 0
        self.entries = []

    def log(self, entry):
        self.entries.append(entry)

    def flush(self):
        self.flushed += 1

    def close(self):
        self.closed_calls += 1


@pytest.fixture
def fake_handler(core_mod):
    made = []

    def factory(settings):
        c = FakeClient()
        made.append(c)
        return c

    settings = core_mod.load_settings(get=lambda k: None, dsn_override="https://k@h.example")
    h = core_mod.ForkSafeLogTideHandler(settings, client_factory=factory)
    h.made = made
    return h


def test_one_client_per_process(fake_handler, record_factory):
    for _ in range(3):
        fake_handler.emit(record_factory())
    assert len(fake_handler.made) == 1


def test_pid_change_creates_new_client(fake_handler, record_factory, monkeypatch):
    real = os.getpid()
    fake_handler.emit(record_factory())
    parent_client = fake_handler.made[0]

    monkeypatch.setattr(os, "getpid", lambda: real + 1)
    fake_handler.emit(record_factory())

    assert len(fake_handler.made) == 2
    assert fake_handler.get_client() is fake_handler.made[1]
    # The inherited client is disarmed, never flushed or closed from the child.
    assert parent_client._closed is True
    assert parent_client._buffer == []
    assert parent_client.flushed == 0 and parent_client.closed_calls == 0


def test_close_client_skips_inherited_client(fake_handler, record_factory, monkeypatch):
    real = os.getpid()
    fake_handler.emit(record_factory())
    client = fake_handler.made[0]
    monkeypatch.setattr(os, "getpid", lambda: real + 1)
    fake_handler.close_client()
    assert client.closed_calls == 0
    monkeypatch.setattr(os, "getpid", lambda: real)
    fake_handler.close_client()
    assert client.closed_calls == 1


def test_after_fork_resets_state(fake_handler, record_factory):
    fake_handler.emit(record_factory())
    old_lock = fake_handler._lock
    old = fake_handler.made[0]
    fake_handler._reset_after_fork()
    assert fake_handler._client is None and fake_handler._pid is None
    assert fake_handler._lock is not old_lock
    assert old._closed and old.closed_calls == 0
    fake_handler.emit(record_factory())
    assert len(fake_handler.made) == 2


def test_failed_client_creation_is_not_retried_per_record(core_mod, record_factory):
    calls = []

    def boom(settings):
        calls.append(1)
        raise RuntimeError("nope")

    settings = core_mod.load_settings(get=lambda k: None, dsn_override="https://k@h.example")
    h = core_mod.ForkSafeLogTideHandler(settings, client_factory=boom)
    for _ in range(3):
        h.emit(record_factory())
    assert len(calls) == 1


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_real_fork_gets_own_client(core_mod, captured):
    core_mod.setup(get={"logtide_dsn": "https://k@h.example"}.get, client_factory=captured.factory)
    handler = core_mod.get_handler()
    parent_client = handler.get_client()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            child_client = handler.get_client()
            os.write(w, b"1" if child_client is not parent_client else b"0")
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    assert os.read(r, 1) == b"1"
