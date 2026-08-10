# -*- coding: utf-8 -*-
"""Twee doodlopende wegen die elke dag iteraties kostten.

Taak 743: een weigering op leesrechten, gevolgd door delegeren naar een
collega met dezelfde beperking. Taak 746: een activiteit plannen op een
model dat geen activiteiten kent, drie dagen op rij.
"""
from odoo.tests import common, tagged

from odoo.addons.daadit_ai_mistral.services import tool_dispatch as td


@tagged("post_install", "-at_install", "daadit_ai")
class TestDelegationPrecheck(common.TransactionCase):

    def setUp(self):
        super().setUp()
        Agent = self.env["ai.agent"]
        Model = self.env["ir.model"]
        self.move_line = Model.search(
            [("model", "=", "account.move.line")], limit=1)
        self.partner = Model.search([("model", "=", "res.partner")], limit=1)
        self.eva = Agent.create({
            "name": "Eva",
            "daadit_allowed_model_ids": [(6, 0, self.partner.ids)],
        })
        self.bram = Agent.create({
            "name": "Bram",
            "daadit_allowed_model_ids": [(6, 0, self.partner.ids)],
        })
        self.floris = Agent.create({
            "name": "Floris",
            "daadit_allowed_model_ids": [
                (6, 0, (self.partner | self.move_line).ids)],
        })
        td.router_state.denied_models = set()

    def tearDown(self):
        td.router_state.denied_models = set()
        super().tearDown()

    def test_nothing_denied_means_no_objection(self):
        self.assertEqual(self.eva._daadit_denied_for_target(self.bram), set())

    def test_receiver_with_the_same_limit_is_pointless(self):
        """Run 558: Eva geweigerd op account.move.line, daarna Bram
        gevraagd — die mag het ook niet."""
        td.router_state.denied_models = {"account.move.line"}
        self.assertEqual(
            self.eva._daadit_denied_for_target(self.bram),
            {"account.move.line"},
        )

    def test_receiver_who_may_read_it_is_a_valid_hop(self):
        """De delegatie mag niet geblokkeerd worden als de ontvanger het
        wél mag — dat is precies waar delegeren voor bestaat."""
        td.router_state.denied_models = {"account.move.line"}
        self.assertEqual(
            self.eva._daadit_denied_for_target(self.floris), set(),
        )

    def test_an_unrestricted_receiver_is_a_valid_hop(self):
        open_agent = self.env["ai.agent"].create({"name": "Open"})
        td.router_state.denied_models = {"account.move.line"}
        self.assertEqual(
            self.eva._daadit_denied_for_target(open_agent), set(),
        )


@tagged("post_install", "-at_install", "daadit_ai")
class TestActivityOnUnsupportedModel(common.TransactionCase):

    def setUp(self):
        super().setUp()
        self.agent = self.env["ai.agent"].create({"name": "Sem"})

    def test_the_refusal_names_an_alternative(self):
        """Sem's run 496/508/536: dezelfde weigering drie dagen op rij,
        omdat de melding alleen zei wat niet kan."""
        res = self.agent._ai_tool_schedule_activity(
            model_name="ir.ui.view", record_id=1, summary="LCP te hoog",
        )
        self.assertIn("error", res)
        self.assertIn("project.task", res["error"])
        self.assertIn("do not retry it on this model", res["error"])

    def test_a_model_with_chatter_is_pointed_at_its_chatter(self):
        hint = self.agent._daadit_no_activity_hint("res.partner")
        self.assertIn("chatter", hint)

    def test_a_model_that_does_support_activities_is_untouched(self):
        """De weigering mag niet in de weg gaan zitten bij een model dat
        activiteiten wél ondersteunt."""
        partner = self.env["res.partner"].create({"name": "Testklant"})
        res = self.agent._ai_tool_schedule_activity(
            model_name="res.partner", record_id=partner.id,
            summary="Opvolgen",
        )
        self.assertTrue(res.get("ok"), res)
        self.assertNotIn("error", res)
