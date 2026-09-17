# -*- coding: utf-8 -*-
"""De blauwdruk van Bo komt uit code en landt op het catalogusrecord.

Wat hier bewezen wordt: de opdracht wordt vervangen, toegang komt er
alleen bij (een handmatig geblokkeerd model blijft dicht, een extra veld
op de privacylijst blijft staan), de nieuwe finance-skills hangen aan
Bo, en een tweede keer toepassen verandert niets meer.
"""
from unittest.mock import patch

from odoo.tests import common, tagged

from ..services import bo_blueprint


@tagged("post_install", "-at_install", "daadit_ai_mistral")
class TestBoBlueprint(common.TransactionCase):

    def setUp(self):
        super().setUp()
        self.Agent = self.env["ai.agent"]
        self.env["ir.config_parameter"].sudo().set_param(
            bo_blueprint.CONFIG_KEY, "0",
        )
        self.bo = self.Agent.search([("name", "=", "Bo")], limit=1)
        if not self.bo:
            self.bo = self.Agent.create({"name": "Bo"})
        self.bo.write({
            "system_prompt": "Oude opdracht uit de database.",
            "daadit_field_blocklist": "res.partner.bank_ids,res.partner.vat",
        })

    def _model(self, name):
        return self.env["ir.model"].search([("model", "=", name)], limit=1)

    def test_opdracht_modellen_en_skills_komen_uit_de_blauwdruk(self):
        with self._alleen_aanwezige_modellen():
            result = self.Agent._daadit_apply_bo_blueprint()
        self.assertTrue(result["applied"])
        self.assertTrue(result["prompt"])
        self.assertIn("Bo, boekhouder", self.bo.system_prompt)
        allowed = set(self.bo.daadit_allowed_model_ids.mapped("model"))
        for name in ("account.move", "account.journal", "res.partner"):
            self.assertIn(name, allowed)
        codes = set(self.bo.daadit_skill_ids.mapped("code"))
        self.assertLessEqual({
            "finance.ledger_check", "finance.sequence_check",
            "finance.bank_health", "finance.reconciliation_backlog",
            "finance.billing_pipeline", "finance.config_health",
        }, codes)
        self.assertIn(
            "account.move",
            self.bo.daadit_activity_scope_ids.mapped("model_name"),
        )
        self.assertEqual(
            self.Agent._daadit_bo_blueprint_applied_version(),
            bo_blueprint.BLUEPRINT_VERSION,
        )

    def test_handmatige_beperkingen_blijven_staan(self):
        journal = self._model("account.journal")
        self.bo.daadit_blocked_model_ids = [(6, 0, journal.ids)]
        self.Agent._daadit_apply_bo_blueprint()
        self.assertNotIn(
            "account.journal",
            self.bo.daadit_allowed_model_ids.mapped("model"),
        )
        self.assertIn("account.journal",
                      self.bo.daadit_blocked_model_ids.mapped("model"))
        self.assertIn("res.partner.vat", self.bo.daadit_field_blocklist)
        self.assertIn("res.partner.bank_ids", self.bo.daadit_field_blocklist)

    def _alleen_aanwezige_modellen(self):
        aanwezig = self.env["ir.model"].search([
            ("model", "in", list(bo_blueprint.ALLOWED_MODELS)),
        ]).mapped("model")
        return patch.object(
            bo_blueprint, "ALLOWED_MODELS", tuple(sorted(aanwezig)),
        )

    def test_tweede_keer_toepassen_doet_niets(self):
        with self._alleen_aanwezige_modellen():
            self.Agent._daadit_apply_bo_blueprint()
            again = self.Agent._daadit_apply_bo_blueprint()
        self.assertFalse(again["applied"])
        with self._alleen_aanwezige_modellen():
            forced = self.Agent._daadit_apply_bo_blueprint(force=True)
        self.assertTrue(forced["applied"])
        self.assertFalse(forced["prompt"])
        self.assertEqual(forced["models"], [])
        self.assertEqual(forced["skills"], [])

    def test_een_ontbrekend_model_wordt_bij_de_volgende_ronde_alsnog_opgepikt(self):
        """Zonder Verkoop geïnstalleerd blijft sale.order ontbreken; de
        versie wordt dan niet vastgelegd, zodat een latere ronde het model
        alsnog toevoegt zodra de app er is."""
        with patch.object(
            bo_blueprint, "ALLOWED_MODELS",
            ("res.partner", "x.nog.niet.geinstalleerd"),
        ):
            first = self.Agent._daadit_apply_bo_blueprint()
        self.assertTrue(first["applied"])
        self.assertEqual(first["missing_models"], ["x.nog.niet.geinstalleerd"])
        self.assertEqual(self.Agent._daadit_bo_blueprint_applied_version(), 0)

        with patch.object(
            bo_blueprint, "ALLOWED_MODELS", ("res.partner", "res.company"),
        ):
            second = self.Agent._daadit_apply_bo_blueprint()
        self.assertTrue(second["applied"])
        self.assertIn("res.company", second["models"])
        self.assertEqual(second["missing_models"], [])
        self.assertEqual(
            self.Agent._daadit_bo_blueprint_applied_version(),
            bo_blueprint.BLUEPRINT_VERSION,
        )

    def test_een_klantexemplaar_is_niet_de_catalogus(self):
        if "daadit_hire_id" not in self.Agent._fields:
            self.skipTest("daadit_agent_hire niet geïnstalleerd")
        catalog = self.Agent._daadit_bo_catalog_agent()
        self.assertEqual(catalog, self.bo)
        self.assertFalse(catalog.daadit_hire_id)
