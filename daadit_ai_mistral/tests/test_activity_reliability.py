# -*- coding: utf-8 -*-
"""P0 regressions: honest write envelopes, restlijst refresh, self-heal."""
from odoo import fields
from odoo.tests import common, tagged


@tagged("post_install", "-at_install", "daadit_ai")
class TestActivityReliability(common.TransactionCase):

    def setUp(self):
        super().setUp()
        self.Agent = self.env["ai.agent"]
        self.agent = self.Agent.create({"name": "Reliability (test)"})
        self.partner = self.env["res.partner"].create({
            "name": "Reliability partner (test)",
        })
        self.assignee = self.env.user

    def _schedule(self, summary, note=""):
        return self.agent._ai_tool_schedule_activity(
            model_name="res.partner",
            record_id=self.partner.id,
            summary=summary,
            note=note,
            user_id=self.assignee.id,
        )

    def test_self_heal_recognises_emoji_prefix(self):
        self.assertTrue(
            self.Agent._daadit_is_self_heal(
                "⚠️ AUTO-APPLY niet toepasbaar",
            ),
        )
        self.assertTrue(
            self.Agent._daadit_is_self_heal("AUTO-APPLY blok 3"),
        )
        self.assertFalse(
            self.Agent._daadit_is_self_heal("Gewone herinnering"),
        )

    def test_duplicate_skip_is_not_ok(self):
        first = self._schedule("Ticket wacht op triage")
        self.assertTrue(first["ok"])
        self.assertTrue(first.get("written"))
        again = self._schedule("Ticket wacht op triage")
        self.assertFalse(again["ok"])
        self.assertFalse(again["written"])
        self.assertEqual(again["reason"], "duplicate")
        self.assertNotIn("activity_id", again)
        self.assertEqual(again["existing_activity_id"], first["activity_id"])

    def test_restlijst_refreshes_instead_of_skipping(self):
        first = self._schedule(
            "Restlijst assurance 2026-08-02: 1 onopgeloste runfout",
            note="<p>run 500</p>",
        )
        self.assertTrue(first["ok"])
        activity_id = first["activity_id"]
        today = fields.Date.context_today(self.agent).strftime("%Y-%m-%d")
        second = self._schedule(
            "Restlijst assurance %s: 6 onopgeloste runfouten" % today,
            note="<p>runs 585-597</p>",
        )
        self.assertTrue(second["ok"])
        self.assertTrue(second.get("written"))
        self.assertTrue(second.get("updated"))
        self.assertEqual(second["activity_id"], activity_id)
        activity = self.env["mail.activity"].browse(activity_id)
        self.assertIn("6 onopgeloste", activity.summary)
        self.assertIn("585-597", activity.note or "")

    def test_emoji_auto_apply_bypasses_open_cap(self):
        # Fill the per-record cap with ordinary todos.
        self.env["ir.config_parameter"].sudo().set_param(
            "daadit_ai_mistral.open_activities_per_record", "1",
        )
        blocked = self._schedule("Eerste herinnering")
        self.assertTrue(blocked["ok"])
        capped = self._schedule("Tweede herinnering zonder note")
        self.assertFalse(capped["ok"])
        self.assertEqual(capped["reason"], "open_activity_cap")

        heal = self._schedule("⚠️ AUTO-APPLY: herstel blok X")
        self.assertTrue(heal["ok"], heal)
        self.assertTrue(heal.get("written") or heal.get("activity_id"))
