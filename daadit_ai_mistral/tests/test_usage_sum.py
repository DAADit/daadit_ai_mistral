# -*- coding: utf-8 -*-
"""Tokens worden over alle rondes van een run opgeteld (v19.0.10.5.0).

Helpdesk-runs 2452-2455 hadden 15 rondes; alleen de laatste telde mee.
"""
from odoo.tests import common, tagged

from ..models import mistral_usage
from ..services.llm_api_patch import _add_usage


@tagged("post_install", "-at_install")
class TestUsageSum(common.BaseCase):

    def test_alle_rondes_tellen_mee(self):
        total = {"prompt_tokens": 0, "completion_tokens": 0}
        for prompt, completion in ((4000, 30), (6000, 40), (9000, 200)):
            _add_usage(total, {"usage": {"prompt_tokens": prompt,
                                         "completion_tokens": completion}})
        self.assertEqual(total, {"prompt_tokens": 19000,
                                 "completion_tokens": 270})

    def test_antwoord_zonder_verbruik_breekt_niets(self):
        total = {"prompt_tokens": 5, "completion_tokens": 1}
        _add_usage(total, None)
        _add_usage(total, {"usage": None})
        _add_usage(total, {"usage": {"prompt_tokens": "x"}})
        self.assertEqual(total, {"prompt_tokens": 5, "completion_tokens": 1})

    def test_prijs_van_medium_en_large_volgt_de_huidige_modellen(self):
        table = mistral_usage._PRICING_USD_PER_1M
        self.assertEqual(table["mistral-medium-latest"], (1.5, 7.5))
        self.assertEqual(table["mistral-large-latest"], (0.5, 1.5))


@tagged("post_install", "-at_install")
class TestOnbekendModel(common.TransactionCase):
    """Een model zonder prijs telt niet als gratis (v19.0.10.5.1)."""

    def _laatste(self, model):
        return self.env["daadit_ai_mistral.usage"].search(
            [("model", "=", model)], order="id desc", limit=1)

    def test_onbekend_model_krijgt_het_duurste_tarief(self):
        self.env["daadit_ai_mistral.usage"].record_usage(
            model="mistral-bestaat-niet-9", prompt_tokens=1_000_000,
            completion_tokens=0,
        )
        row = self._laatste("mistral-bestaat-niet-9")
        duurste_in, _uit = mistral_usage._fallback_unit_prices()
        self.assertTrue(row.price_fallback)
        self.assertAlmostEqual(row.estimated_cost_usd, duurste_in, places=4)
        self.assertGreater(row.estimated_cost_usd, 0.0)

    def test_bekend_model_houdt_zijn_eigen_prijs(self):
        self.env["daadit_ai_mistral.usage"].record_usage(
            model="mistral-large-latest", prompt_tokens=1_000_000,
            completion_tokens=0,
        )
        row = self._laatste("mistral-large-latest")
        self.assertFalse(row.price_fallback)
        self.assertAlmostEqual(row.estimated_cost_usd, 0.5, places=4)

    def test_embedding_met_alleen_een_invoerprijs_is_bekend(self):
        self.env["daadit_ai_mistral.usage"].record_usage(
            kind="embedding", model="mistral-embed",
            prompt_tokens=1_000_000, completion_tokens=0,
        )
        row = self._laatste("mistral-embed")
        self.assertFalse(row.price_fallback)
