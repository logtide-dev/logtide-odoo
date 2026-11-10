from odoo import api, models

from .. import core


class IrCron(models.Model):
    _inherit = "ir.cron"

    @api.model
    def _callback(self, *args, **kwargs):
        # The signature differs between Odoo versions (17 passes a job id as a
        # third argument), so only the cron name is extracted and everything
        # else is passed through untouched.
        name = kwargs.get("cron_name")
        if name is None and args and isinstance(args[0], str):
            name = args[0]
        with core.cron_context(name):
            return super()._callback(*args, **kwargs)
