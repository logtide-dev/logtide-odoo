"""Load logtide_odoo/core.py directly so the tests never import Odoo.

Importing ``logtide_odoo.core`` would run the package ``__init__``, which
imports the Odoo models. core.py has no relative imports, so it is loaded
as a standalone module instead.
"""

import importlib.util
import logging
import pathlib
import sys
import threading
import types

import pytest

CORE_PATH = pathlib.Path(__file__).resolve().parent.parent / "logtide_odoo" / "core.py"


def _load_core():
    spec = importlib.util.spec_from_file_location("logtide_odoo_core", CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["logtide_odoo_core"] = module
    spec.loader.exec_module(module)
    return module


core = _load_core()

DSN = "https://lp_testkey@logs.example.com"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    core._reset_for_tests()
    for _conf_key, env_key in core.CONFIG_KEYS.values():
        monkeypatch.delenv(env_key, raising=False)
    yield
    core._reset_for_tests()


@pytest.fixture
def core_mod():
    return core


class Captured:
    """Collects the entries the SDK client would have buffered."""

    def __init__(self):
        self.entries = []
        self.clients = []

    def factory(self, settings):
        from logtide_sdk.client import LogTideClient

        options = core.build_client_options(settings)
        options.flush_interval = 0

        def before_send(entry):
            self.entries.append(entry)
            return None  # drop: nothing goes to the network

        options.before_send = before_send
        client = LogTideClient(options)
        self.clients.append(client)
        return client


@pytest.fixture
def captured():
    return Captured()


@pytest.fixture
def handler(captured):
    settings = core.load_settings(get=lambda k: {"logtide_dsn": DSN}.get(k))
    h = core.ForkSafeLogTideHandler(settings, client_factory=captured.factory)
    yield h
    h.close()


def make_record(msg="hello", name="odoo.test", level=logging.INFO, exc_info=None):
    return logging.LogRecord(name, level, __file__, 10, msg, None, exc_info)


@pytest.fixture
def record_factory():
    return make_record


@pytest.fixture
def fake_odoo_http(monkeypatch):
    """Install a fake ``odoo.http`` module; returns a setter for ``request``."""
    module = types.ModuleType("odoo.http")
    module.request = None
    monkeypatch.setitem(sys.modules, "odoo.http", module)

    def set_request(req):
        module.request = req

    return set_request


@pytest.fixture
def thread_attrs(monkeypatch):
    thread = threading.current_thread()

    def set_attrs(**attrs):
        for k, v in attrs.items():
            monkeypatch.setattr(thread, k, v, raising=False)

    return set_attrs
