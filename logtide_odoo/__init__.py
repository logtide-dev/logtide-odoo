from . import core
from . import models


def post_load():
    """Runs for server-wide modules (``--load=base,web,logtide_odoo``)."""
    core.setup()


# Fallback for the "normally installed module" case, where post_load is not
# executed. setup() is idempotent. A missing DSN is not reported here: the
# System Parameter ``logtide.dsn`` may still provide one once the registry loads.
core.setup(defer_missing=True)
