from odoo import models

from .. import core


class IrConfigParameter(models.Model):
    _inherit = "ir.config_parameter"

    def _register_hook(self):
        res = super()._register_hook()
        # Last-resort DSN source (Odoo.sh): System Parameter ``logtide.dsn``.
        # No-op when a DSN was already resolved from odoo.conf / environment.
        core.registry_ready(lambda: self.sudo().get_param("logtide.dsn"))
        return res
