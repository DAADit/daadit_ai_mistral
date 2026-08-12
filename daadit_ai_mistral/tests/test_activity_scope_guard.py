# -*- coding: utf-8 -*-
"""Het pad dat de tool werkelijk loopt: weigeren of plannen.

De vorige ronde legde de grens vast als records; deze ronde neemt de
serveractie die grens ook echt over. Wat hier getest wordt is dus niet
alleen de beslissing, maar het gedrag eromheen dat tot nu toe alleen in
een databaseveld bestond: de eis van een samenvatting, de logging bij een
weigering, en het reparatiekanaal van de assurance-watchdog dat langs de
cap op open herinneringen mag.
"""
from odoo.addons.daadit_ai_mistral.models.ai_agent_activity_scope import (
    REPAIR_CHANNEL_BACKLOG, SERVER_ACTION_CODE,
)
from odoo.tests import common, tagged


@tagged("post_install", "-at_install", "daadit_ai")
class TestActivityScopeGuard(common.TransactionCase):

    def setUp(self):
        super().setUp()
        if "knowledge.article" not in self.env:
            self.skipTest("knowledge.article is hier niet beschikbaar")
        self.Agent = self.env["ai.agent"]
        self.Scope = self.env["daadit.ai.agent.activity.scope"]
        self.Article = self.env["knowledge.article"]
        self.postbus = self.Article.create({"name": "Conceptenbak (test)"})
        self.lux = self.Agent.create({"name": "Lux (test)"})
        self.Scope.create({
            "agent_id": self.lux.id,
            "model_name": "knowledge.article",
            "record_domain": '[["id", "=", %s]]' % self.postbus.id,
        })

    def _plan(self, agent, **kwargs):
        vals = {
            "model_name": "knowledge.article",
            "record_id": self.postbus.id,
            "summary": "Concept: drie LinkedIn-posts",
        }
        vals.update(kwargs)
        return agent._daadit_schedule_activity_guarded(**vals)

    def test_a_destination_outside_the_scope_is_refused_before_the_orm(self):
        result = self._plan(self.lux, model_name="website.page", record_id=1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked_by_scope_guard"])
        self.assertIn("geen activiteit dragen", result["error"])
        self.assertIn("knowledge.article", result["error"])

    def test_an_activity_without_a_summary_helps_nobody(self):
        result = self._plan(self.lux, summary="   ")
        self.assertFalse(result["ok"])
        self.assertIn("zonder samenvatting", result["error"])

    def test_a_planned_activity_lands_on_the_postbox(self):
        result = self._plan(self.lux)
        self.assertTrue(result.get("ok"), result)
        activity = self.env["mail.activity"].search([
            ("res_model", "=", "knowledge.article"),
            ("res_id", "=", self.postbus.id),
        ], limit=1)
        self.assertTrue(activity)
        self.assertEqual(activity.summary, "Concept: drie LinkedIn-posts")

    def test_the_repair_channel_only_exists_for_the_watchdog(self):
        """Zonder de vlag is AUTO-APPLY gewoon een activiteit."""
        self.assertFalse(self.lux.daadit_repair_channel)
        result = self._plan(self.lux, summary="AUTO-APPLY blok 3")
        self.assertNotIn("bypassed_reminder_cap", result)

    def test_the_watchdog_may_propose_a_repair_past_the_reminder_cap(self):
        watchdog = self.Agent.create({
            "name": "Argus (test)", "daadit_repair_channel": True})
        self.Scope.create({
            "agent_id": watchdog.id,
            "model_name": "knowledge.article",
            "record_domain": '[["id", "=", %s]]' % self.postbus.id,
        })
        result = self._plan(watchdog, summary="AUTO-APPLY blok 3")
        self.assertTrue(result["ok"])
        self.assertTrue(result["bypassed_reminder_cap"])

        # Exact hetzelfde voorstel nog een keer levert geen tweede
        # activiteit op — anders loopt het artikel vol met duplicaten.
        again = self._plan(watchdog, summary="AUTO-APPLY blok 3")
        self.assertFalse(again["ok"])
        self.assertFalse(again.get("written", True))
        self.assertTrue(again["skipped"])
        self.assertEqual(again["reason"], "duplicate_autoapply")
        self.assertEqual(
            again["existing_activity_id"], result["activity_id"],
        )
        self.assertNotIn("activity_id", again)

    def test_a_backlog_of_proposals_is_a_failure_of_the_applier(self):
        watchdog = self.Agent.create({
            "name": "Argus (test)", "daadit_repair_channel": True})
        self.Scope.create({
            "agent_id": watchdog.id,
            "model_name": "knowledge.article",
            "record_domain": '[["id", "=", %s]]' % self.postbus.id,
        })
        for i in range(REPAIR_CHANNEL_BACKLOG):
            self.assertTrue(
                self._plan(watchdog, summary="AUTO-APPLY blok %s" % i)["ok"])
        result = self._plan(watchdog, summary="AUTO-APPLY blok laatste")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "applier_backlog")

    def test_the_repair_channel_does_not_widen_the_write_scope(self):
        """De uitzondering geldt de cap, niet de grens."""
        watchdog = self.Agent.create({
            "name": "Argus (test)", "daadit_repair_channel": True})
        self.Scope.create({
            "agent_id": watchdog.id,
            "model_name": "knowledge.article",
            "record_domain": '[["id", "=", %s]]' % self.postbus.id,
        })
        elders = self.Article.create({"name": "Buiten scope (test)"})
        result = self._plan(
            watchdog, record_id=elders.id, summary="AUTO-APPLY blok 3")
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked_by_scope_guard"])

    def test_the_server_action_code_decides_nothing_itself(self):
        """De actie mag een doorgeefluik zijn en verder niets."""
        self.assertIn(
            "_daadit_schedule_activity_guarded", SERVER_ACTION_CODE)
        self.assertNotIn("SCOPE-GUARD:", SERVER_ACTION_CODE)
        for own_decision in ("knowledge.article", "helpdesk.ticket",
                             "website.page", "allowed = True"):
            self.assertNotIn(own_decision, SERVER_ACTION_CODE)
