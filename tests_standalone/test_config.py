import logging

from conftest import DSN


def test_defaults(core_mod):
    s = core_mod.load_settings(get=lambda k: None)
    assert s.dsn is None
    assert s.service == "odoo"
    assert s.level == logging.INFO
    assert s.environment is None
    assert s.capture_ir_logging is False


def test_conf_values(core_mod):
    conf = {
        "logtide_dsn": DSN,
        "logtide_service": "erp",
        "logtide_level": "warning",
        "logtide_environment": "staging",
        "logtide_capture_ir_logging": "True",
    }
    s = core_mod.load_settings(get=conf.get)
    assert (s.dsn, s.service, s.level, s.environment) == (DSN, "erp", logging.WARNING, "staging")
    assert s.capture_ir_logging is True


def test_env_fallback(core_mod, monkeypatch):
    monkeypatch.setenv("LOGTIDE_DSN", DSN)
    monkeypatch.setenv("LOGTIDE_SERVICE", "from-env")
    monkeypatch.setenv("LOGTIDE_LEVEL", "ERROR")
    monkeypatch.setenv("LOGTIDE_ENVIRONMENT", "prod")
    monkeypatch.setenv("LOGTIDE_CAPTURE_IR_LOGGING", "1")
    s = core_mod.load_settings(get=lambda k: False)  # odoo returns False for unset options
    assert (s.dsn, s.service, s.level, s.environment) == (DSN, "from-env", logging.ERROR, "prod")
    assert s.capture_ir_logging is True


def test_conf_wins_over_env(core_mod, monkeypatch):
    monkeypatch.setenv("LOGTIDE_SERVICE", "from-env")
    s = core_mod.load_settings(get={"logtide_service": "from-conf"}.get)
    assert s.service == "from-conf"


def test_param_dsn_is_last_resort(core_mod):
    s = core_mod.load_settings(get=lambda k: None, dsn_override="https://k@param.example")
    assert s.dsn == "https://k@param.example"
    s = core_mod.load_settings(get={"logtide_dsn": DSN}.get, dsn_override="https://k@param.example")
    assert s.dsn == DSN


def test_level_parsing(core_mod):
    assert core_mod.parse_level("WARN") == (logging.WARNING, True)
    assert core_mod.parse_level("10") == (10, True)
    assert core_mod.parse_level("nonsense") == (logging.INFO, False)
    s = core_mod.load_settings(get={"logtide_level": "nonsense"}.get)
    assert s.level == logging.INFO and s.problems


def test_missing_dsn_is_noop_and_warns_once(core_mod, caplog):
    root_handlers = list(logging.getLogger().handlers)
    with caplog.at_level(logging.WARNING, logger="logtide_odoo"):
        assert core_mod.setup(get=lambda k: None) is None
        assert core_mod.setup(get=lambda k: None) is None
    warnings = [r for r in caplog.records if r.name == "logtide_odoo"]
    assert len(warnings) == 1
    assert core_mod.get_handler() is None
    assert logging.getLogger().handlers == root_handlers


def test_missing_dsn_deferred_does_not_warn(core_mod, caplog):
    with caplog.at_level(logging.WARNING, logger="logtide_odoo"):
        assert core_mod.setup(get=lambda k: None, defer_missing=True) is None
    assert not [r for r in caplog.records if r.name == "logtide_odoo"]


def test_invalid_dsn_disables(core_mod):
    assert core_mod.setup(get={"logtide_dsn": "not-a-dsn"}.get) is None
    assert core_mod.get_handler() is None


def test_setup_is_idempotent(core_mod):
    get = {"logtide_dsn": DSN}.get
    first = core_mod.setup(get=get)
    second = core_mod.setup(get=get)
    assert first is second is core_mod.get_handler()
    assert logging.getLogger().handlers.count(first) == 1
    logging.getLogger().removeHandler(first)  # simulate Odoo resetting root handlers
    assert core_mod.setup(get=get) is first
    assert logging.getLogger().handlers.count(first) == 1


def test_registry_ready_uses_system_parameter(core_mod):
    assert core_mod.setup(get=lambda k: None, defer_missing=True) is None
    core_mod.registry_ready(lambda: DSN)
    assert core_mod.get_handler() is not None


def test_registry_ready_ignored_when_already_configured(core_mod):
    core_mod.setup(get={"logtide_dsn": DSN}.get)
    called = []
    core_mod.registry_ready(lambda: called.append(1))
    assert not called
