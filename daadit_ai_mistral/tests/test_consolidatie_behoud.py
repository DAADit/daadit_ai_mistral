# -*- coding: utf-8 -*-
"""De twee dingen die deze lijn had en de deploy-lijn niet (OAS 711).

Bij de consolidatie is de deploy-lijn leidend, dus alles wat hier uniek
was liep het risico weggeschreven te worden. Twee dingen zijn bewust
opnieuw toegepast; deze tests zorgen dat een volgende overname ze niet
alsnog verliest.
"""
import json

from odoo.tests import common, tagged

from odoo.addons.daadit_ai_mistral.services import llm_api_patch as lap
from odoo.addons.daadit_ai_mistral.services import tool_dispatch as td


class _FakeSchemaAction:
    """Stand-in voor een serveractie die de operator zelf maakte."""

    name = "Blogconcept"
    ai_tool_description = "Schrijf een blogconcept"

    def __init__(self, properties):
        self.ai_tool_schema = json.dumps({
            "type": "object",
            "properties": {p: {"type": "string"} for p in properties},
            "required": list(properties),
        })

    def sudo(self):
        return self


@tagged("post_install", "-at_install", "daadit_ai")
class TestEigenToolKrijgtZijnSchema(common.TransactionCase):
    """Zonder schema roept het model een eigen tool aan met ``{}``."""

    def _annotate(self, action):
        origineel = td._resolve_tool_action
        td._resolve_tool_action = lambda agent, name: action
        try:
            return td.annotate_tools(["action_1226"], agent=object())
        finally:
            td._resolve_tool_action = origineel

    def test_het_schema_van_de_actie_wordt_geadverteerd(self):
        defs = self._annotate(_FakeSchemaAction(["topic_hint"]))
        params = defs[0]["function"]["parameters"]
        self.assertIn("topic_hint", params["properties"])
        self.assertFalse(
            params["additionalProperties"],
            "verzonnen argumenten horen expliciet niet welkom te zijn",
        )
        self.assertEqual(
            defs[0]["function"]["description"], "Schrijf een blogconcept",
        )

    def test_zonder_agent_blijft_het_de_stub(self):
        defs = td.annotate_tools(["action_1226"])
        self.assertEqual(defs[0]["function"]["parameters"]["properties"], {})

    def test_een_onleesbaar_schema_valt_terug_op_de_stub(self):
        kapot = _FakeSchemaAction(["topic_hint"])
        kapot.ai_tool_schema = "{geen json"
        defs = self._annotate(kapot)
        self.assertEqual(defs[0]["function"]["parameters"]["properties"], {})

    def test_een_bekende_tool_houdt_zijn_eigen_schema(self):
        naam = next(iter(td.TOOL_SCHEMAS))
        defs = td.annotate_tools([naam], agent=object())
        self.assertEqual(
            defs[0]["function"]["parameters"],
            td.TOOL_SCHEMAS[naam]["parameters"],
        )


@tagged("post_install", "-at_install", "daadit_ai")
class TestTaalreferentie(common.TransactionCase):
    """Een bare "?" als laatste beurt liet een Nederlands gesprek in het
    Frans antwoorden; te korte berichten tellen daarom niet mee."""

    def test_te_kort_telt_niet_als_taalsignaal(self):
        self.assertLess(lap._letter_count("?"), lap.MIN_LANG_REF_LETTERS)
        self.assertLess(lap._letter_count("ok"), lap.MIN_LANG_REF_LETTERS)
        self.assertGreaterEqual(
            lap._letter_count("Kun je de openstaande tickets tonen?"),
            lap.MIN_LANG_REF_LETTERS,
        )

    def test_zonder_bruikbare_referentie_blijft_de_tekst_ongewijzigd(self):
        tekst = "Access blocked by your administrator."
        self.assertEqual(
            lap._translate_to_chat_language(
                None, "mistral-small",
                [{"role": "user", "content": "?"}],
                tekst,
            ),
            tekst,
            "vertalen zonder taalsignaal is gokken; dan liever Engels",
        )
