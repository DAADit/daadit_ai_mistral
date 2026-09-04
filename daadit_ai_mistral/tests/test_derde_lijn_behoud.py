# -*- coding: utf-8 -*-
"""Ronde 2 van OAS 711: er was een derde lijn, en die is hier leidend.

De lijn die op productie draait (`adriedaadit/daadit`) had werk dat noch
deze repo noch de fork kende. Bij het overnemen liep het omgekeerde
risico: dat het werk van déze repo eronder verdween. Deze tests noemen
van beide kanten één ding dat bij een volgende overname niet stil mag
sneuvelen.
"""
from odoo.tests import common, tagged

from odoo.addons.daadit_ai_mistral.services import llm_api_patch as lap
from odoo.addons.daadit_ai_mistral.services import tool_dispatch as td


@tagged("post_install", "-at_install", "daadit_ai")
class TestDerdeLijnAanwezig(common.TransactionCase):
    """Wat alleen de productielijn had, hoort hier nu te staan."""

    def test_projectrapportage_geldt_als_schrijfactie(self):
        for slug in (
            "ir_actions_server_project_goal_set",
            "ir_actions_server_project_phase_upsert",
            "ir_actions_server_project_report",
        ):
            self.assertIn(
                slug, td.WRITE_SIDE_TOOL_SLUGS,
                "rapporteren schrijft in Odoo; buiten de schrijfset "
                "ontsnapt het aan de governance-guards",
            )

    def test_de_projectmanager_kan_rapporteren(self):
        agent = self.env["ai.agent"]
        for method in (
            "_ai_tool_project_goal_set",
            "_ai_tool_project_phase_upsert",
            "_ai_tool_project_report",
        ):
            self.assertTrue(
                hasattr(agent, method),
                "%s hoort bij de projectrapportage uit de "
                "productielijn" % method,
            )

    def test_schrijfscope_geldt_ook_voor_toewijzen(self):
        """Taak 1079: assign_user vroeg de schrijfscope niet op."""
        import inspect
        src = inspect.getsource(
            self.env["ai.agent"].__class__._ai_tool_assign_user
        )
        self.assertIn("_daadit_write_scope", src)


@tagged("post_install", "-at_install", "daadit_ai")
class TestProductLijnBehouden(common.TransactionCase):
    """Wat alleen deze repo had, staat er na de overname nog."""

    def test_eigen_tool_houdt_zijn_schema(self):
        self.assertTrue(hasattr(td, "_custom_tool_definition"))
        self.assertIn(
            "agent", td.annotate_tools.__code__.co_varnames,
            "zonder agent kan een zelfgemaakte tool zijn schema niet "
            "ophalen en roept het model hem met {} aan",
        )

    def test_tussenstand_tijdens_delegatie(self):
        agent = self.env["ai.agent"]
        self.assertTrue(hasattr(agent, "_daadit_post_channel_status"))
        self.assertIn(
            "Sem",
            agent._daadit_delegation_status_body("Sem"),
            "de melding hoort te zeggen bij wie het wordt nagevraagd",
        )

    def test_taalreferentiedrempel_staat_er_nog(self):
        self.assertGreater(lap.MIN_LANG_REF_LETTERS, 2)
        self.assertLess(lap._letter_count("ok"), lap.MIN_LANG_REF_LETTERS)
