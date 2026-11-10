# LogTide for Odoo

Forwards Odoo server logs to [LogTide](https://logtide.dev) using the official [`logtide-sdk`](https://pypi.org/project/logtide-sdk/) Python SDK.

- This branch targets Odoo 17.0. Each Odoo series has its own branch: 17.0, 18.0, 19.0, 20.0.
- Safe with prefork workers (`workers > 0`), cron workers and the gevent longpolling process: each process owns its own client, and buffered logs are flushed when a worker exits or is recycled.
- Every record carries Odoo context: database, user, process, HTTP request, JSON-RPC model and method, cron job name, and the incoming W3C `traceparent` trace id.
- Optional forwarding of `ir.logging` rows.

## Install

```bash
pip install logtide-sdk
```

Copy the `logtide_odoo` directory into a folder of your addons path. On Odoo.sh, add `logtide-sdk` to the `requirements.txt` at the root of your repository and commit the addon there.

## Configure

Add the DSN of your LogTide project to `odoo.conf`:

```ini
[options]
logtide_dsn = https://lp_yourkey@logs.example.com
logtide_service = odoo
logtide_level = INFO
logtide_environment = production
```

| Option (`odoo.conf`) | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `logtide_dsn` | `LOGTIDE_DSN` | none | Required. `https://<api_key>@<host>[/base-path]`. Without it the addon does nothing and logs one warning. |
| `logtide_service` | `LOGTIDE_SERVICE` | `odoo` | Service name attached to every entry. |
| `logtide_level` | `LOGTIDE_LEVEL` | `INFO` | Minimum level forwarded (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). |
| `logtide_environment` | `LOGTIDE_ENVIRONMENT` | none | Added to every entry as the `environment` metadata key. |
| `logtide_capture_ir_logging` | `LOGTIDE_CAPTURE_IR_LOGGING` | `False` | Also forward rows created in `ir.logging` (see below). |

`odoo.conf` wins over the environment. If no DSN is found in either, the System Parameter `logtide.dsn` is used (see Odoo.sh).

Note that `logtide_level` only filters what Odoo already produces. To receive `DEBUG` records, Odoo's own log level must allow them (`--log-level=debug` or `log_handler`).

## Activate

### Server-wide module (recommended)

Load the addon before the registry starts, so logs from startup are captured:

```ini
[options]
server_wide_modules = base,web,logtide_odoo
```

or `--load=base,web,logtide_odoo` on the command line. Nothing needs to be installed in a database for this mode.

### Installed module

Alternatively, install "LogTide" from the Apps menu. Forwarding then starts when the module is imported, which is slightly after startup.

### Odoo.sh

You cannot edit `odoo.conf` or set environment variables on Odoo.sh. Install the module normally and create a System Parameter (Settings, Technical, System Parameters) with key `logtide.dsn` and your DSN as value. Restart the instance. This fallback is read once, when the first registry has loaded, and is only used if no DSN is set in `odoo.conf` or the environment. Logs from before that point are not forwarded.

## Metadata reference

Fields added to every entry (when available):

| Key | Source |
| --- | --- |
| `odoo.dbname` | Database of the current thread or request. |
| `odoo.uid` | User id of the current thread or session. |
| `odoo.pid` | OS process id that produced the record. |
| `http.path` | Request path (only inside an HTTP request). |
| `http.method` | Request method. |
| `http.remote_addr` | Remote address as seen by Odoo (behind a proxy this is the proxy unless `proxy_mode` is set). |
| `odoo.model` | Model of a `/web/dataset/call_kw` JSON-RPC call. |
| `odoo.method` | Method of a `/web/dataset/call_kw` JSON-RPC call. |
| `odoo.cron` | Name of the scheduled action being run (cron workers). |
| `environment` | `logtide_environment`, if set. |

The SDK adds the usual `logger`, `module`, `funcName`, `lineno`, `pathname` and, for records with a traceback, a structured `exception` key. The traceback also stays in the message.

If the request carries a valid W3C `traceparent` header, its trace id is set on every entry logged during that request (`trace_id` field). Requests without one get no trace id.

Rows forwarded from `ir.logging` carry the same context plus `ir_logging.type` and `source` (`ir.logging`).

## ir.logging forwarding

With `logtide_capture_ir_logging = True`, rows created through the ORM in `ir.logging` (name, type, level, message, path, func, line, dbname) are sent to LogTide as well. Off by default.

Odoo's own `log_db` feature writes its rows with raw SQL rather than the ORM, so they should not be forwarded twice; do not enable both if you see duplicates.

## How it behaves

- The handler is installed on the root logger once per process, from the `post_load` hook and, as a fallback, on module import. Setup is idempotent.
- The client is created lazily on first use in each process and re-created if the process id changes. A client inherited from a parent through `fork` is disarmed without being flushed, so entries are never sent twice.
- Records from `logtide_sdk`, `urllib3` and `requests` are never forwarded, and the SDK is never re-entered from its own emit.
- Logs are batched by the SDK (default flush every 5 seconds). A worker killed with SIGKILL can lose the last few seconds.
- A failure to reach LogTide never raises into Odoo.

## Troubleshooting

- **Nothing arrives.** Check the startup log for `logtide_odoo` warnings: missing or invalid DSN, or `logtide-sdk` not installed in the Python environment Odoo runs in.
- **No logs from startup.** Use server-wide mode.
- **Only INFO and above.** Raise Odoo's log level and set `logtide_level = DEBUG`.
- **Duplicate entries after fork.** Should not happen; if it does, open an issue with your server mode (`workers`, gevent) and Odoo version.
- **No `odoo.cron` key.** Only logs emitted while an `ir.cron` job runs through `ir.cron._callback` get it. Code that runs outside that call does not.
- **Odoo.sh DSN not picked up.** The module must be installed in the database and the parameter key must be exactly `logtide.dsn`.

## Development

Core logic lives in `logtide_odoo/core.py` and does not import Odoo, so it can be tested without it:

```bash
python -m venv .venv
.venv/bin/pip install -e path/to/logtide-sdk-python pytest   # or: pip install logtide-sdk pytest
.venv/bin/pytest
```

The Odoo test case in `logtide_odoo/tests/` runs with `odoo-bin -i logtide_odoo --test-tags /logtide_odoo`.

## License

MIT, see [LICENSE](LICENSE).
