# -*- coding: utf-8 -*-
"""De twee bevindingen van de zelfherstelloop (taken 1077 en 1078).

1077 wilde een samenvatting van minimaal twaalf tekens. Dat criterium
meet het verkeerde: de Vault Agent schreef in productie "Openstaand
actiepunt afhandelen" (31 tekens) en dat zegt nog steeds niet waar het
over gaat. Wat hier getest wordt is daarom inhoud, niet lengte.

1078 wilde de schrijfscope verruimen naar de hele Vault-boom. De
scoperegel van de Vault Agent gebruikt al ``root_article_id = 191``, dus
de boom inclusief subartikelen valt er al onder; wat ontbrak was bewijs
dat dat ook zo werkt en een weigering die zegt waar het wél kan.
"""
from odoo.addons.daadit_ai_mistral.models.ai_agent_activity_scope import (
    SEED_ACTIVITY_SCOPES,
)
from odoo.tests import common, tagged


@tagged("post_install", "-at_install", "daadit_ai")
class TestSummaryMustNameItsSubject(common.TransactionCase):

    def setUp(self):
        super().setUp()
        if "knowledge.article" not in self.env:
            self.skipTest("knowledge.article is hier niet beschikbaar")
        self.Agent = self.env["ai.agent"]
        self.article = self.env["knowledge.article"].create({
            "name": "Vault-artikel (test)",
        })
        self.vera = self.Agent.create({"name": "Vera (test)"})
        self.env["daadit.ai.agent.activity.scope"].create({
            "agent_id": self.vera.id,
            "model_name": "knowledge.article",
            "record_domain": '[["id", "=", %s]]' % self.article.id,
        })

    def _plan(self, summary):
        return self.vera._daadit_schedule_activity_guarded(
            model_name="knowledge.article",
            record_id=self.article.id,
            summary=summary,
        )

    def test_a_summary_that_only_says_that_something_must_happen_is_refused(
            self):
        """Precies de samenvattingen die de bevinding aanwees."""
        for hollow in ("Opvolgen", "Actie vereist", "Actie vereist nu",
                       "Openstaand actiepunt afhandelen",
                       "Deze taak graag oppakken", "Follow up required"):
            result = self._plan(hollow)
            self.assertFalse(result["ok"], hollow)
            self.assertTrue(result["blocked_by_scope_guard"], hollow)
            self.assertEqual(result["reason"], "summary_zonder_inhoud")
            self.assertIn("benoemt geen onderwerp", result["error"])

    def test_the_refusal_says_what_is_missing_and_not_how_long_it_must_be(self):
        result = self._plan("Actie vereist nu")
        self.assertIn("de lengte is niet het probleem", result["error"])
        self.assertNotIn("teken", result["error"])

    def test_an_empty_summary_keeps_its_own_refusal(self):
        result = self._plan("   ")
        self.assertEqual(result["reason"], "summary_leeg")
        self.assertIn("zonder samenvatting", result["error"])

    def test_a_summary_that_names_its_subject_passes(self):
        for real in ("SLA-bijlage mist",
                     "Incidentrapport 2026-07 zonder eigenaar",
                     "Audit-log januari onvolledig",
                     "Isolatie.com: contract loopt af"):
            result = self._plan(real)
            self.assertTrue(result.get("ok"), (real, result))
            self.env["mail.activity"].search([
                ("res_model", "=", "knowledge.article"),
                ("res_id", "=", self.article.id),
            ]).unlink()

    def test_length_is_not_the_criterion(self):
        """Kort met onderwerp mag, lang zonder onderwerp niet.

        "SLA mist" (8 tekens) zou de voorgestelde eis van twaalf tekens
        niet halen, "Openstaand actiepunt afhandelen" (31) juist wel —
        omgekeerd aan wat de ontvanger eraan heeft.
        """
        self.assertFalse(self.Agent._daadit_summary_is_hollow("SLA mist"))
        self.assertTrue(self.Agent._daadit_summary_is_hollow(
            "Openstaand actiepunt afhandelen"))

    def test_the_repair_channel_prefix_is_not_content_by_itself(self):
        """Het kanaal zegt waar het bericht vandaan komt, niet wat er is."""
        self.assertTrue(self.Agent._daadit_summary_is_hollow(
            "⚠️ AUTO-APPLY niet toepasbaar: opvolgen"))
        self.assertFalse(self.Agent._daadit_summary_is_hollow(
            "AUTO-APPLY blok 2: samenvattingseis promptregistry"))


@tagged("post_install", "-at_install", "daadit_ai")
class TestVaultTreeScope(common.TransactionCase):
    """Bevinding 1078: de boom onder de Vault-root, en niets daarbuiten."""

    def setUp(self):
        super().setUp()
        if "knowledge.article" not in self.env:
            self.skipTest("knowledge.article is hier niet beschikbaar")
        self.Article = self.env["knowledge.article"]
        self.root = self.Article.create({"name": "Vault (test)"})
        self.child = self.Article.create({
            "name": "Vault/technical (test)", "parent_id": self.root.id,
        })
        self.grandchild = self.Article.create({
            "name": "Vault/technical/audit-logs (test)",
            "parent_id": self.child.id,
        })
        self.elders = self.Article.create({"name": "Buiten de Vault (test)"})
        self.vera = self.env["ai.agent"].create({"name": "Vera (test)"})
        self.env["daadit.ai.agent.activity.scope"].create({
            "agent_id": self.vera.id,
            "model_name": "knowledge.article",
            "record_domain":
                '[["root_article_id", "=", %s]]' % self.root.id,
        })

    def _plan(self, record, summary="Audit-log januari onvolledig"):
        return self.vera._daadit_schedule_activity_guarded(
            model_name="knowledge.article",
            record_id=record.id,
            summary=summary,
        )

    def test_a_write_deeper_in_the_tree_is_allowed(self):
        """Wat de Vault Agent in productie feitelijk doet: subartikelen."""
        for target in (self.root, self.child, self.grandchild):
            result = self._plan(target)
            self.assertTrue(result.get("ok"), (target.name, result))

    def test_a_write_outside_the_tree_is_refused_before_the_orm(self):
        result = self._plan(self.elders)
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked_by_scope_guard"])
        self.assertEqual(result["reason"], "scope")
        self.assertFalse(self.elders.activity_ids)

    def test_the_refusal_names_where_it_can_be_written(self):
        """Een weigering die alleen "buiten de scope" zegt laat gokken."""
        result = self._plan(self.elders)
        self.assertIn("knowledge.article", result["error"])
        self.assertIn("root_article_id", result["error"])
        self.assertIn(str(self.root.id), result["error"])

    def test_the_seeded_scope_of_the_vault_agent_is_the_whole_tree(self):
        """De vastgelegde regel gebruikt de root, niet een lijst ids."""
        self.assertEqual(
            SEED_ACTIVITY_SCOPES["Vera"],
            [("knowledge.article", [["root_article_id", "=", 191]])],
        )
