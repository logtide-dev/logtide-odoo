from odoo import api, models

from .. import core


class IrLogging(models.Model):
    _inherit = "ir.logging"

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if core.ir_logging_enabled():
            for vals in vals_list:
                core.forward_ir_logging(vals)
        return records
