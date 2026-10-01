# -*- coding: utf-8 -*-
"""Logregels committen de cursor van de aanroeper niet (taak 1488)."""
from unittest.mock import patch

from odoo.tests import common, tagged

from ..services import tool_dispatch

_NAME = "daadit_ai_mistral.test_log_cursor"


@tagged("post_install", "-at_install")
class TestLogCursor(common.TransactionCase):

    def _count(self):
        # Een eigen cursor: de rij is op een eigen cursor gecommit en
        # valt buiten de momentopname van de testtransactie.
        with self.env.registry.cursor() as cr:
            cr.execute("SELECT count(*) FROM ir_logging WHERE name = %s",
                       [_NAME])
            return cr.fetchone()[0]

    def _cleanup(self):
        with self.env.registry.cursor() as cr:
            cr.execute("DELETE FROM ir_logging WHERE name = %s", [_NAME])

    def test_logregel_zonder_commit_op_de_run(self):
        def no_commit():
            raise AssertionError("de cursor van de run is gecommit")

        self.addCleanup(self._cleanup)
        before = self._count()
        with patch.object(self.env.cr, "commit", no_commit):
            tool_dispatch._record_in_ir_logging(
                self.env, "WARNING", _NAME, "proef",
            )
        self.assertEqual(self._count(), before + 1)
