# -*- coding: utf-8 -*-
"""De schrijfgrens voor activiteiten, per agent vastgelegd.

Deze grens leefde als Python in het ``code``-veld van serveractie 1142:
geen versiebeheer, geen review, geen test, en bij een nieuwe
klantdatabase bestond hij niet. Hier staat hij als records, met per
agentrol een test die vastlegt wat mag en wat niet.

Twee gevallen komen rechtstreeks uit productie:

* Sem en Lux mochten ``website.page``, een model zonder
  ``mail.activity.mixin`` — drie mislukte tool-acties per dag, voorstel
  weg. Dat moet een uitleg geven, geen crash.
* Een agent zonder scoperegels mag niets, ook niet als de prompt hem
  ergens naartoe stuurt.
"""
from odoo.tests import common, tagged


@tagged("post_install", "-at_install", "daadit_ai")
class TestActivityScope(common.TransactionCase):

    def setUp(self):
        super().setUp()
        self.Agent = self.env["ai.agent"]
        self.Scope = self.env["daadit.ai.agent.activity.scope"]
        self.Article = self.env["knowledge.article"]
        self.postbus = self.Article.create({"name": "Conceptenbak (test)"})
        self.elders = self.Article.create({"name": "Ander artikel (test)"})
        self.lux = self.Agent.create({"name": "Lux (test)"})
        self.Scope.create({
            "agent_id": self.lux.id,
            "model_name": "knowledge.article",
            "record_domain": '[["id", "=", %s]]' % self.postbus.id,
        })

    def test_an_agent_without_scope_lines_may_do_nothing(self):
        naked = self.Agent.create({"name": "Zonder scope (test)"})
        allowed, reason = naked._daadit_activity_scope(
            "knowledge.article", self.postbus.id)
        self.assertFalse(allowed)
        self.assertIn("geen schrijfscope", reason)

    def test_the_postbox_is_the_one_place_that_is_allowed(self):
        allowed, reason = self.lux._daadit_activity_scope(
            "knowledge.article", self.postbus.id)
        self.assertTrue(allowed)
        self.assertFalse(reason)

    def test_another_article_falls_outside_the_scope(self):
        allowed, reason = self.lux._daadit_activity_scope(
            "knowledge.article", self.elders.id)
        self.assertFalse(allowed)
        self.assertIn("buiten de schrijfscope", reason)

    def test_a_model_that_cannot_carry_an_activity_is_refused_with_the_alternative(self):
        """Sem/Lux liepen elke dag op ``website.page`` stuk: de guard
        stond het toe en Odoo weigerde het daarna zelf. De weigering
        noemt nu de bestemming die wél werkt."""
        self.assertFalse(self.lux._daadit_activity_capable("website.page"))
        allowed, reason = self.lux._daadit_activity_scope("website.page", 1)
        self.assertFalse(allowed)
        self.assertIn("geen activiteit dragen", reason)
        self.assertIn("knowledge.article", reason)

    def test_a_model_outside_the_scope_names_what_is_allowed(self):
        allowed, reason = self.lux._daadit_activity_scope(
            "res.users", self.env.user.id)
        self.assertFalse(allowed)
        self.assertIn("knowledge.article", reason)

    def test_an_id_that_does_not_exist_is_refused(self):
        allowed, reason = self.lux._daadit_activity_scope(
            "knowledge.article", 0)
        self.assertFalse(allowed)
        self.assertIn("bestaat niet", reason)

    def test_an_inactive_line_does_not_grant_anything(self):
        self.lux.daadit_activity_scope_ids.active = False
        allowed, reason = self.lux._daadit_activity_scope(
            "knowledge.article", self.postbus.id)
        self.assertFalse(allowed)
        self.assertIn("geen schrijfscope", reason)

    def test_a_scope_without_a_domain_allows_every_record_of_that_model(self):
        pim = self.Agent.create({"name": "Pim (test)"})
        self.Scope.create({
            "agent_id": pim.id, "model_name": "project.task",
            "record_domain": "[]",
        })
        task = self.env["project.task"].create({"name": "Taak (test)"})
        allowed, _reason = pim._daadit_activity_scope(
            "project.task", task.id)
        self.assertTrue(allowed)
        blocked, reason = pim._daadit_activity_scope(
            "knowledge.article", self.postbus.id)
        self.assertFalse(blocked)
        self.assertIn("project.task", reason)

    def test_seeding_is_idempotent_and_never_widens(self):
        """De seeding mag een handmatige aanscherping niet terugdraaien."""
        before = self.lux.daadit_activity_scope_ids.mapped("record_domain")
        self.Agent._daadit_seed_activity_scopes()
        self.Agent._daadit_seed_activity_scopes()
        self.assertEqual(
            self.lux.daadit_activity_scope_ids.mapped("record_domain"),
            before,
        )
