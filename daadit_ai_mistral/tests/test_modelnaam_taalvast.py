# -*- coding: utf-8 -*-
"""Run 1677 (Bo, 16-9): vijf keer "verkeerde naam voor een onderdeel".

Bo vroeg de velden op van ``account_move``, ``res_partner`` enzovoort —
tabelnamen in plaats van modelnamen. De dispatcher weigerde terecht dat
die niet bestaan, maar wist net zo goed dat ``account.move`` wél bestaat.
Eén-op-één omzetten van ``_`` naar ``.`` is veilig zolang het resultaat
een geregistreerd model is; alles anders blijft onbekend.

In dezelfde run heette de tool ``ir_actions_server_velden_oproepen``: de
naam van de actie werd in de taal van de gebruiker geslugd. De slug hoort
uit de bronnaam (Engels) te komen, anders herkent geen scope- of
schrijflijst hem.
"""
from odoo.tests import common, tagged

from odoo.addons.daadit_ai_mistral.services import tool_dispatch as td
from odoo.addons.daadit_ai_mistral.services.llm_api_patch import (
    _slug_tool_name,
)


@tagged("post_install", "-at_install", "daadit_ai")
class TestModelnaamMetUnderscores(common.TransactionCase):

    def test_tabelnaam_wordt_modelnaam(self):
        self.assertEqual(
            td._dotted_model_name(self.env, "res_partner"), "res.partner",
        )
        self.assertEqual(
            td._dotted_model_name(self.env, "ir_model_fields"),
            "ir.model.fields",
        )

    def test_verzonnen_naam_blijft_onbekend(self):
        self.assertEqual(td._dotted_model_name(self.env, "mail_tag"), "")
        self.assertEqual(td._dotted_model_name(self.env, "res_partner_x"), "")

    def test_puntnaam_of_zonder_underscore_wordt_niet_aangeraakt(self):
        self.assertEqual(td._dotted_model_name(self.env, "res.partner"), "")
        self.assertEqual(td._dotted_model_name(self.env, "partner"), "")
        self.assertEqual(td._dotted_model_name(self.env, ""), "")


@tagged("post_install", "-at_install", "daadit_ai")
class TestToolnaamTaalvast(common.TransactionCase):

    def test_slug_uit_bronnaam_ongeacht_taal(self):
        action = self.env["ir.actions.server"].create({
            "name": "AI: Get Fields",
            "model_id": self.env["ir.model"]._get_id("res.partner"),
            "state": "code",
            "code": "",
        })
        nl = self.env["res.lang"].sudo()._activate_lang("nl_NL")
        self.assertTrue(nl)
        action.with_context(lang="nl_NL").name = "AI: Velden oproepen"
        self.assertEqual(
            action.with_context(lang="nl_NL").name, "AI: Velden oproepen",
        )
        self.assertEqual(
            _slug_tool_name(
                action.with_context(lang="nl_NL")
                .with_context(lang="en_US").name
            ),
            "ir_actions_server_get_fields",
        )
        self.assertEqual(
            _slug_tool_name(action.with_context(lang="nl_NL").name),
            "ir_actions_server_velden_oproepen",
        )
