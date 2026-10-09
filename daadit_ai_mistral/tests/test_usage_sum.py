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
