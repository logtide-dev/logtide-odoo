import logging
import sys
from types import SimpleNamespace


def test_excluded_loggers_never_forwarded(handler, captured, record_factory):
    for name in ("logtide_sdk", "logtide_sdk.client", "urllib3.connectionpool", "urllib3", "requests.sessions"):
        handler.handle(record_factory(name=name, level=logging.ERROR))
    assert captured.entries == []
    assert captured.clients == []  # not even a client was created
    handler.handle(record_factory(name="odoo.http"))
    handler.handle(record_factory(name="urllib3x"))  # prefix match is on dotted boundaries
    assert len(captured.entries) == 2


def test_level_threshold(core_mod, captured, record_factory):
    settings = core_mod.load_settings(
        get={"logtide_dsn": "https://k@h.example", "logtide_level": "WARNING"}.get
    )
    h = core_mod.ForkSafeLogTideHandler(settings, client_factory=captured.factory)
    log = logging.getLogger("logtide_odoo_test.threshold")
    log.propagate = False
    log.setLevel(logging.DEBUG)
    log.addHandler(h)
    try:
        log.info("below")
        log.warning("at level")
    finally:
        log.removeHandler(h)
    assert [e.message for e in captured.entries] == ["at level"]
    h.close()


def test_recursion_guard(core_mod, record_factory):
    seen = []

    class ReentrantClient:
        _closed = False
        _buffer = []
        _flush_timer = None

        def log(self, entry):
            seen.append(entry)
            h.handle(record_factory(msg="nested"))  # would recurse forever without the guard

        def close(self):
            pass

    settings = core_mod.load_settings(get=lambda k: None, dsn_override="https://k@h.example")
    h = core_mod.ForkSafeLogTideHandler(settings, client_factory=lambda s: ReentrantClient())
    h.handle(record_factory(msg="outer"))
    assert [e.message for e in seen] == ["outer"]
    h.handle(record_factory(msg="again"))  # the guard is released afterwards
    assert len(seen) == 2


def test_basic_metadata_and_environment(core_mod, captured, record_factory, thread_attrs):
    thread_attrs(dbname="proddb", uid=7)
    settings = core_mod.load_settings(
        get={
            "logtide_dsn": "https://k@h.example",
            "logtide_environment": "staging",
            "logtide_service": "erp",
        }.get
    )
    h = core_mod.ForkSafeLogTideHandler(settings, client_factory=captured.factory)
    h.handle(record_factory(msg="hi"))
    e = captured.entries[0]
    assert e.service == "erp"
    assert e.metadata["odoo.dbname"] == "proddb"
    assert e.metadata["odoo.uid"] == 7
    assert e.metadata["odoo.pid"] > 0
    assert e.metadata["environment"] == "staging"
    assert e.metadata["logger"] == "odoo.test"
    h.close()


def test_no_context_when_nothing_is_set(handler, captured, record_factory, thread_attrs):
    thread_attrs(dbname=None, uid=None)
    handler.handle(record_factory())
    meta = captured.entries[0].metadata
    assert "odoo.dbname" not in meta and "odoo.uid" not in meta and "http.path" not in meta
    assert "odoo.pid" in meta


def _request(path="/web/dataset/call_kw/res.partner/search_read", params=None, headers=None):
    return SimpleNamespace(
        httprequest=SimpleNamespace(
            path=path, method="POST", remote_addr="10.0.0.5", headers=headers or {}
        ),
        params=params if params is not None else {},
        session=SimpleNamespace(uid=3),
        db="reqdb",
    )


def test_http_call_kw_enrichment(handler, captured, record_factory, fake_odoo_http, thread_attrs):
    thread_attrs(dbname=None, uid=None)
    fake_odoo_http(_request(params={"model": "sale.order", "method": "write"}))
    handler.handle(record_factory())
    m = captured.entries[0].metadata
    assert m["http.path"] == "/web/dataset/call_kw/res.partner/search_read"
    assert m["http.method"] == "POST"
    assert m["http.remote_addr"] == "10.0.0.5"
    assert m["odoo.model"] == "sale.order" and m["odoo.method"] == "write"
    assert m["odoo.dbname"] == "reqdb" and m["odoo.uid"] == 3


def test_call_kw_model_method_from_path_when_params_missing(
    handler, captured, record_factory, fake_odoo_http
):
    fake_odoo_http(_request(params={}))
    handler.handle(record_factory())
    m = captured.entries[0].metadata
    assert m["odoo.model"] == "res.partner" and m["odoo.method"] == "search_read"


def test_plain_http_has_no_rpc_fields(handler, captured, record_factory, fake_odoo_http):
    fake_odoo_http(_request(path="/web/login"))
    handler.handle(record_factory())
    m = captured.entries[0].metadata
    assert m["http.path"] == "/web/login" and "odoo.model" not in m


def test_unbound_request_is_ignored(handler, captured, record_factory, fake_odoo_http):
    class Unbound:
        def __bool__(self):
            return False

        def __getattr__(self, name):
            raise RuntimeError("object is not bound")

    fake_odoo_http(Unbound())
    handler.handle(record_factory())
    assert "http.path" not in captured.entries[0].metadata

    class Raising:
        def __getattr__(self, name):
            raise RuntimeError("object is not bound")

    fake_odoo_http(Raising())
    handler.handle(record_factory())
    assert len(captured.entries) == 2


def test_partially_initialized_request(handler, captured, record_factory, fake_odoo_http):
    fake_odoo_http(SimpleNamespace(httprequest=SimpleNamespace(path="/x")))
    handler.handle(record_factory())
    m = captured.entries[0].metadata
    assert m["http.path"] == "/x" and "http.method" not in m


def test_traceparent_sets_trace_id(handler, captured, record_factory, fake_odoo_http):
    tid = "4bf92f3577b34da6a3ce929d0e0e4736"
    fake_odoo_http(_request(headers={"traceparent": f"00-{tid}-00f067aa0ba902b7-01"}))
    handler.handle(record_factory())
    assert captured.entries[0].trace_id == tid
    # the scope is restored after the record: no leak into the next one
    fake_odoo_http(_request(headers={}))
    handler.handle(record_factory())
    assert captured.entries[1].trace_id is None


def test_malformed_traceparent_is_ignored(handler, captured, record_factory, fake_odoo_http):
    fake_odoo_http(_request(headers={"traceparent": "garbage"}))
    handler.handle(record_factory())
    assert captured.entries[0].trace_id is None


def test_cron_name(handler, captured, record_factory, core_mod):
    with core_mod.cron_context("Mail: queue"):
        handler.handle(record_factory())
    handler.handle(record_factory())
    assert captured.entries[0].metadata["odoo.cron"] == "Mail: queue"
    assert "odoo.cron" not in captured.entries[1].metadata


def test_exception_traceback_is_kept(handler, captured, record_factory):
    try:
        raise ValueError("boom")
    except ValueError:
        rec = record_factory(
            msg="Exception during request handling.",
            name="odoo.http",
            level=logging.ERROR,
            exc_info=sys.exc_info(),
        )
    handler.handle(rec)
    e = captured.entries[0]
    assert "exception" in e.metadata
    assert "Traceback" in e.message and "ValueError: boom" in e.message


def test_ir_logging_forwarding(core_mod, captured):
    settings = core_mod.load_settings(
        get={"logtide_dsn": "https://k@h.example", "logtide_capture_ir_logging": True}.get
    )
    h = core_mod.ForkSafeLogTideHandler(settings, client_factory=captured.factory)
    h.send_row(
        {
            "name": "my.module",
            "type": "server",
            "level": "ERROR",
            "message": "row msg",
            "path": "/a.py",
            "func": "f",
            "line": "12",
            "dbname": "db1",
        }
    )
    h.send_row({"name": "x", "type": "server", "level": "DEBUG", "message": "below threshold"})
    assert len(captured.entries) == 1
    e = captured.entries[0]
    assert e.message == "row msg" and e.level.value == "error"
    assert e.metadata["odoo.dbname"] == "db1"
    assert e.metadata["ir_logging.type"] == "server" and e.metadata["source"] == "ir.logging"
    h.close()


def test_ir_logging_enabled_flag(core_mod):
    assert core_mod.ir_logging_enabled() is False
    core_mod.setup(
        get={"logtide_dsn": "https://k@h.example", "logtide_capture_ir_logging": "true"}.get
    )
    assert core_mod.ir_logging_enabled() is True
