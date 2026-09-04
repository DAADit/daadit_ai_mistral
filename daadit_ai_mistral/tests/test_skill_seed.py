# -*- coding: utf-8 -*-
"""Wie welke skills krijgt bij het zaaien van de catalogus.

Orderverwerking hoort bij sales, dus bij Sanne; factureren blijft bij
Marit. Het zaaien is additief: een eerder handmatig gekozen skill mag
er niet door verdwijnen.
"""
from odoo.tests import common, tagged


@tagged("post_install", "-at_install", "daadit_ai_mistral")
class TestSkillSeed(common.TransactionCase):

    def setUp(self):
        super().setUp()
        self.Agent = self.env["ai.agent"]

    def _seed(self, name):
        """De seed pakt de agent met die naam; maak hem alleen als nodig."""
        agent = self.Agent.search([("name", "=", name)], limit=1)
        if not agent:
            agent = self.Agent.create({"name": name})
        self.Agent._daadit_seed_skills()
        return agent

    def test_sanne_werkt_van_lead_tot_levering(self):
        sanne = self._seed("Sanne")
        codes = set(sanne.daadit_skill_ids.mapped("code"))
        self.assertLessEqual({
            "sales.order_intake_check",
            "sales.order_price_variance",
            "sales.order_stock_check",
            "sales.order_delivery_watch",
            "sales.order_invoice_handover",
        }, codes)
        self.assertNotIn("finance.invoice_candidates", codes)

    def test_factureren_blijft_bij_marit(self):
        marit = self._seed("Marit")
        codes = set(marit.daadit_skill_ids.mapped("code"))
        self.assertIn("finance.invoice_candidates", codes)
        self.assertFalse(
            {code for code in codes if code.startswith("sales.order")}
        )

    def test_zaaien_is_additief(self):
        sanne = self.Agent.search([("name", "=", "Sanne")], limit=1)
        if not sanne:
            sanne = self.Agent.create({"name": "Sanne"})
        ledger = self.env.ref("daadit_ai_mistral.skill_finance_ledger_check")
        sanne.daadit_skill_ids = [(6, 0, ledger.ids)]
        self.Agent._daadit_seed_skills()
        codes = set(sanne.daadit_skill_ids.mapped("code"))
        self.assertIn("finance.ledger_check", codes)
        self.assertIn("sales.order_intake_check", codes)
