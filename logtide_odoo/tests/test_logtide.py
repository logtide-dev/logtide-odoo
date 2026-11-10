from unittest import mock

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.logtide_odoo import core


@tagged("post_install", "-at_install")
class TestLogTide(TransactionCase):
    def _row(self):
        return {
            "name": "logtide.test",
            "type": "server",
            "dbname": self.env.cr.dbname,
            "level": "INFO",
            "message": "hello from ir.logging",
            "path": "logtide_odoo/tests/test_logtide.py",
            "func": "_row",
            "line": "1",
        }

    def test_ir_logging_not_forwarded_by_default(self):
        with mock.patch.object(core, "ir_logging_enabled", return_value=False), \
                mock.patch.object(core, "forward_ir_logging") as forward:
            self.env["ir.logging"].create(self._row())
        forward.assert_not_called()

    def test_ir_logging_forwarded_when_enabled(self):
        with mock.patch.object(core, "ir_logging_enabled", return_value=True), \
                mock.patch.object(core, "forward_ir_logging") as forward:
            record = self.env["ir.logging"].create(self._row())
        self.assertTrue(record)
        forward.assert_called_once()
        self.assertEqual(forward.call_args[0][0]["message"], "hello from ir.logging")

    def test_dsn_system_parameter_fallback(self):
        self.env["ir.config_parameter"].sudo().set_param("logtide.dsn", "https://lp_key@logs.example.com")
        with mock.patch.object(core, "_handler", None), \
                mock.patch.object(core, "setup") as setup:
            self.env["ir.config_parameter"]._register_hook()
        setup.assert_called_once_with(dsn_override="https://lp_key@logs.example.com")

    def test_cron_context_is_cleared(self):
        with core.cron_context("My job"):
            self.assertEqual(core.current_cron_name(), "My job")
        self.assertIsNone(core.current_cron_name())
