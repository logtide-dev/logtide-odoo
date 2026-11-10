"""Core logic of the LogTide addon.

This module is deliberately free of top-level ``odoo`` imports so it can be
tested without Odoo. Everything Odoo-specific (config, ``odoo.http.request``)
is looked up lazily and guarded.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import logging
import os
import sys
import threading
from dataclasses import dataclass, field

_LOG = logging.getLogger("logtide_odoo")

# Loggers that must never be forwarded: they are used by the SDK's own HTTP
# stack, so forwarding them would feed the client its own traffic.
EXCLUDED_LOGGERS = ("logtide_sdk", "urllib3", "requests")

DEFAULT_SERVICE = "odoo"
DEFAULT_LEVEL = "INFO"
PARAM_DSN = "logtide.dsn"

# option name in odoo.conf -> environment variable
CONFIG_KEYS = {
    "dsn": ("logtide_dsn", "LOGTIDE_DSN"),
    "service": ("logtide_service", "LOGTIDE_SERVICE"),
    "level": ("logtide_level", "LOGTIDE_LEVEL"),
    "environment": ("logtide_environment", "LOGTIDE_ENVIRONMENT"),
    "capture_ir_logging": ("logtide_capture_ir_logging", "LOGTIDE_CAPTURE_IR_LOGGING"),
}


# ---------------------------------------------------------------- settings


@dataclass
class Settings:
    dsn: str | None = None
    service: str = DEFAULT_SERVICE
    level: int = logging.INFO
    environment: str | None = None
    capture_ir_logging: bool = False
    problems: list[str] = field(default_factory=list)


def _clean(value):
    """Normalise a raw config value: unset / False / empty -> None."""
    if value is None or value is False:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _lookup(get, name):
    conf_key, env_key = CONFIG_KEYS[name]
    value = None
    try:
        value = _clean(get(conf_key))
    except Exception:
        value = None
    if value is None:
        value = _clean(os.environ.get(env_key))
    return value


def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y", "t")


def parse_level(value):
    """Return ``(levelno, ok)``. Accepts names (``WARN`` too) and integers."""
    if value is None:
        return logging.INFO, True
    if isinstance(value, int) and not isinstance(value, bool):
        return value, True
    text = str(value).strip().upper()
    if text.isdigit():
        return int(text), True
    text = {"WARN": "WARNING", "FATAL": "CRITICAL"}.get(text, text)
    number = logging.getLevelName(text)
    if isinstance(number, int):
        return number, True
    return logging.INFO, False


def _odoo_config_get(key):
    try:
        from odoo.tools import config
    except Exception:
        return None
    try:
        return config.get(key)
    except Exception:
        try:
            return config.options.get(key)
        except Exception:
            return None


def load_settings(get=None, dsn_override=None) -> Settings:
    """Resolve settings from odoo.conf (via ``get``), then the environment."""
    get = get or _odoo_config_get
    s = Settings()
    s.dsn = _lookup(get, "dsn") or _clean(dsn_override)
    s.service = _lookup(get, "service") or DEFAULT_SERVICE
    raw_level = _lookup(get, "level")
    s.level, ok = parse_level(raw_level)
    if not ok:
        s.problems.append(
            f"invalid logtide_level {raw_level!r}, falling back to {DEFAULT_LEVEL}"
        )
    s.environment = _lookup(get, "environment")
    s.capture_ir_logging = parse_bool(_lookup(get, "capture_ir_logging"))
    return s


# ------------------------------------------------------------- enrichment

_cron_name: contextvars.ContextVar = contextvars.ContextVar("logtide_odoo_cron", default=None)


@contextlib.contextmanager
def cron_context(name):
    token = _cron_name.set(name or None)
    try:
        yield
    finally:
        _cron_name.reset(token)


def current_cron_name():
    return _cron_name.get()


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


def _get_request():
    """Return ``odoo.http.request`` if Odoo is loaded and it is usable."""
    http = sys.modules.get("odoo.http")
    if http is None:
        return None
    req = getattr(http, "request", None)
    if req is None:
        return None
    # An unbound LocalProxy raises on any attribute access (or is falsy).
    if _safe(lambda: bool(req)) is False:
        return None
    return req


def _http_context(req):
    """Return ``(metadata, traceparent_header)`` for a bound request."""
    meta: dict = {}
    httpreq = _safe(lambda: req.httprequest)
    if httpreq is None:
        return meta, None

    path = _safe(lambda: httpreq.path)
    if path:
        meta["http.path"] = str(path)
    method = _safe(lambda: httpreq.method)
    if method:
        meta["http.method"] = str(method)
    remote = _safe(lambda: httpreq.remote_addr)
    if remote:
        meta["http.remote_addr"] = str(remote)

    if path and str(path).startswith("/web/dataset/call_kw"):
        params = _safe(lambda: req.params)
        model = rpc_method = None
        if params is not None and hasattr(params, "get"):
            model = _safe(lambda: params.get("model"))
            rpc_method = _safe(lambda: params.get("method"))
        if not (model and rpc_method):
            # /web/dataset/call_kw/<model>/<method>
            parts = [p for p in str(path).split("/") if p]
            if len(parts) >= 5:
                model = model or parts[3]
                rpc_method = rpc_method or parts[4]
        if model:
            meta["odoo.model"] = str(model)
        if rpc_method:
            meta["odoo.method"] = str(rpc_method)

    traceparent = _safe(lambda: httpreq.headers.get("traceparent"))
    return meta, traceparent if isinstance(traceparent, str) else None


def collect_context(dbname_hint=None):
    """Build the Odoo metadata for the current thread / request.

    Returns ``(metadata, trace_id)``. Never raises.
    """
    meta: dict = {}
    trace_id = None
    try:
        thread = threading.current_thread()
        dbname = getattr(thread, "dbname", None) or dbname_hint
        uid = getattr(thread, "uid", None)

        req = _get_request()
        if req is not None:
            http_meta, traceparent = _http_context(req)
            meta.update(http_meta)
            if not dbname:
                dbname = _safe(lambda: req.db)
            if uid is None:
                uid = _safe(lambda: req.session.uid)
            if traceparent:
                from logtide_sdk.tracecontext import parse_traceparent

                parsed = parse_traceparent(traceparent)
                if parsed is not None:
                    trace_id = parsed.trace_id

        if dbname and isinstance(dbname, str):
            meta["odoo.dbname"] = dbname
        if isinstance(uid, int) and not isinstance(uid, bool):
            meta["odoo.uid"] = uid
        meta["odoo.pid"] = os.getpid()

        cron = current_cron_name()
        if cron:
            meta["odoo.cron"] = cron
    except Exception:
        meta.setdefault("odoo.pid", os.getpid())
    return meta, trace_id


# ---------------------------------------------------------------- handler


class ExcludeLoggersFilter(logging.Filter):
    """Drops records emitted by the SDK's own stack (and its dependencies)."""

    def __init__(self, prefixes=EXCLUDED_LOGGERS):
        super().__init__()
        self.prefixes = tuple(prefixes)

    def filter(self, record):
        name = record.name or ""
        for prefix in self.prefixes:
            if name == prefix or name.startswith(prefix + "."):
                return False
        return True


def build_client_options(settings: Settings):
    from logtide_sdk.models import ClientOptions

    global_metadata = {}
    if settings.environment:
        global_metadata["environment"] = settings.environment
    return ClientOptions(
        dsn=settings.dsn,
        service=settings.service,
        global_metadata=global_metadata,
    )


def _default_client_factory(settings: Settings):
    from logtide_sdk.client import LogTideClient

    return LogTideClient(build_client_options(settings))


def _neutralize_inherited_client(client) -> None:
    """Disarm a client that was created in another process (the parent).

    Its buffer holds entries the parent will send itself, and its sockets are
    shared with the parent: flushing or closing it here would duplicate logs
    or corrupt the parent's connections. So it is only marked closed.
    """
    for action in (
        lambda: setattr(client, "_closed", True),
        lambda: setattr(client, "_buffer", []),
        lambda: client._flush_timer.cancel() if client._flush_timer else None,
        lambda: atexit.unregister(client.close),
    ):
        try:
            action()
        except Exception:
            pass


class ForkSafeLogTideHandler(logging.Handler):
    """``logging.Handler`` that owns one ``LogTideClient`` per process."""

    def __init__(self, settings: Settings, client_factory=None):
        super().__init__(settings.level)
        self.settings = settings
        self._client_factory = client_factory or _default_client_factory
        self._lock = threading.Lock()
        self._guard = threading.local()
        self._pid = None
        self._client = None
        self._inner = None
        self.addFilter(ExcludeLoggersFilter())

    # -- per-process client -------------------------------------------

    def _get_pair(self):
        pid = os.getpid()
        if self._pid == pid:
            return self._client, self._inner
        failure = None
        with self._lock:
            if self._pid != pid:
                if self._client is not None:
                    _neutralize_inherited_client(self._client)
                client = inner = None
                try:
                    from logtide_sdk.handler import LogTideHandler

                    client = self._client_factory(self.settings)
                    inner = LogTideHandler(client=client, service=self.settings.service)
                except Exception as exc:
                    client = inner = None
                    failure = exc
                self._client, self._inner, self._pid = client, inner, pid
            result = (self._client, self._inner)
        if failure is not None:
            # Outside the lock: logging may re-enter this handler's code.
            _LOG.warning("could not create the LogTide client (%s); forwarding disabled in pid %s", failure, pid)
        return result

    def get_client(self):
        return self._get_pair()[0]

    def _reset_after_fork(self) -> None:
        self._lock = threading.Lock()
        self._guard = threading.local()
        if self._client is not None:
            _neutralize_inherited_client(self._client)
        self._client = self._inner = None
        self._pid = None

    def close_client(self) -> None:
        """Flush and close this process's client (no-op for inherited ones)."""
        client = self._client
        if client is not None and self._pid == os.getpid():
            try:
                client.close()
            except Exception:
                pass

    def close(self) -> None:
        self.close_client()
        super().close()

    # -- emission -----------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        guard = self._guard
        if getattr(guard, "active", False):
            return  # re-entrant call from within our own emit
        guard.active = True
        try:
            client, inner = self._get_pair()
            if client is None or inner is None:
                return
            meta, trace_id = collect_context(getattr(record, "dbname", None))
            from logtide_sdk.scope import push_scope

            with push_scope() as scope:
                if trace_id:
                    scope.set_trace_context(trace_id)
                for key, value in meta.items():
                    scope.set_extra(key, value)
                inner.emit(record)
        except Exception:
            self.handleError(record)
        finally:
            guard.active = False

    def send_row(self, vals: dict) -> None:
        """Forward one ``ir.logging`` row. Never raises."""
        guard = self._guard
        if getattr(guard, "active", False):
            return
        guard.active = True
        try:
            client, inner = self._get_pair()
            if client is None or inner is None:
                return
            levelno, _ok = parse_level(vals.get("level") or "INFO")
            if levelno < self.level:
                return
            from logtide_sdk.models import LogEntry
            from logtide_sdk.scope import push_scope

            meta, trace_id = collect_context(vals.get("dbname"))
            meta.update(
                {
                    "logger": vals.get("name"),
                    "pathname": vals.get("path"),
                    "funcName": vals.get("func"),
                    "lineno": vals.get("line"),
                    "ir_logging.type": vals.get("type"),
                    "source": "ir.logging",
                }
            )
            meta = {k: v for k, v in meta.items() if v is not None}
            with push_scope() as scope:
                if trace_id:
                    scope.set_trace_context(trace_id)
                client.log(
                    LogEntry(
                        service=self.settings.service,
                        level=inner._map_level(levelno),
                        message=str(vals.get("message") or ""),
                        metadata=meta,
                    )
                )
        except Exception:
            pass
        finally:
            guard.active = False


# ------------------------------------------------------------- lifecycle

_state_lock = threading.RLock()
_handler: ForkSafeLogTideHandler | None = None
_warned_missing = False
_hooks_registered = False


def get_handler():
    return _handler


def _after_fork_in_child() -> None:
    handler = _handler
    if handler is not None:
        handler._reset_after_fork()


def _shutdown() -> None:
    handler = _handler
    if handler is not None:
        handler.close_client()


def _register_process_hooks() -> None:
    global _hooks_registered
    if _hooks_registered:
        return
    _hooks_registered = True
    atexit.register(_shutdown)
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_after_fork_in_child)


def setup(get=None, dsn_override=None, defer_missing=False, client_factory=None):
    """Install the handler on the root logger. Idempotent.

    Returns the handler, or None when forwarding is not configured.
    """
    global _handler, _warned_missing
    with _state_lock:
        if _handler is not None:
            root = logging.getLogger()
            if _handler not in root.handlers:
                root.addHandler(_handler)
            return _handler

        settings = load_settings(get, dsn_override)
        for problem in settings.problems:
            _LOG.warning(problem)

        if not settings.dsn:
            if not defer_missing and not _warned_missing:
                _warned_missing = True
                _LOG.warning(
                    "no LogTide DSN configured (odoo.conf logtide_dsn, env LOGTIDE_DSN "
                    "or System Parameter logtide.dsn); log forwarding is disabled"
                )
            return None

        try:
            from logtide_sdk.dsn import parse_dsn

            parse_dsn(settings.dsn)
        except ImportError:
            _LOG.warning("logtide-sdk is not installed (pip install logtide-sdk); log forwarding is disabled")
            return None
        except Exception as exc:
            _LOG.warning("invalid LogTide DSN (%s); log forwarding is disabled", exc)
            return None

        handler = ForkSafeLogTideHandler(settings, client_factory=client_factory)
        logging.getLogger().addHandler(handler)
        _handler = handler
        _register_process_hooks()
        return handler


def registry_ready(read_param) -> None:
    """Called when a registry has loaded; DSN fallback from a System Parameter."""
    if _handler is not None:
        return
    dsn = None
    try:
        dsn = read_param()
    except Exception:
        dsn = None
    setup(dsn_override=dsn)


def ir_logging_enabled() -> bool:
    handler = _handler
    return handler is not None and handler.settings.capture_ir_logging


def forward_ir_logging(vals: dict) -> None:
    handler = _handler
    if handler is not None:
        handler.send_row(vals)


def _reset_for_tests() -> None:
    """Remove the installed handler and forget all state (tests only)."""
    global _handler, _warned_missing
    with _state_lock:
        if _handler is not None:
            logging.getLogger().removeHandler(_handler)
            _handler.close_client()
        _handler = None
        _warned_missing = False
