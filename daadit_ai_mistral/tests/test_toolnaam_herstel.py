# -*- coding: utf-8 -*-
"""Een verkeerd genoemde tool hoeft geen kapotte run te zijn."""
import json

from odoo.tests import common, tagged

from odoo.addons.daadit_ai_mistral.services import tool_dispatch as td


class _Action:
    def __init__(self, name, ident):
        self.name = name
        self.id = ident

    def with_env(self, _env):
        return self


class _Actions(list):
    def get_external_id(self):
        return {}

    def filtered(self, func):
        return _Actions(a for a in self if func(a))


class _Agent:
    env = None


@tagged("post_install", "-at_install", "daadit_ai")
class TestToolnaamHerstel(common.BaseCase):

    def _resolve(self, name, actions):
        origineel = td._agent_tool_actions
        td._agent_tool_actions = lambda agent: _Actions(actions)
        try:
            return td._resolve_tool_action(_Agent(), name)
        finally:
            td._agent_tool_actions = origineel

    def test_zoeken_is_de_enige_zoektool(self):
        zoeken = _Action("AI: Administratie Zoeken", 1603)
        modellen = _Action("AI: Administratie Modellen", 1604)
        hit = self._resolve("ir_actions_server_zoeken", [zoeken, modellen])
        self.assertIs(hit, zoeken)

    def test_twee_kandidaten_is_geen_gok(self):
        acts = [
            _Action("AI: Administratie Zoeken", 1),
            _Action("AI: Kennis Zoeken", 2),
        ]
        self.assertIsNone(self._resolve("ir_actions_server_zoeken", acts))

    def test_claims_als_tool_geeft_het_blok_terug(self):
        claims = [{"text": "Niets gevonden", "kind": "waarneming"}]
        result = td.run_tool_call(_Agent(), {"function": {
            "name": "claims", "arguments": json.dumps({"claims": claims}),
        }})
        self.assertTrue(result["ok"])
        self.assertNotIn("error", result)
        self.assertIn("```claims", result["claims_block"])
        self.assertIn("Niets gevonden", result["claims_block"])

    def test_gewone_onbekende_tool_blijft_een_fout(self):
        self.assertIsNone(td.claims_pseudo_tool_result("zoek_alles", {}))
