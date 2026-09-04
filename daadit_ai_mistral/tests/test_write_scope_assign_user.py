# -*- coding: utf-8 -*-
"""De schrijfscope geldt ook voor ``AI: Assign User`` (taak 1079).

Het voorstel uit de zelfherstelloop wilde dat de guard een ticket hard
weigert vóór uitvoering als het gesloten is of in een gevouwen fase
staat — en ook als er geen SLA-deadline is. Het eerste deel bleek een
echt gat: ``_ai_tool_assign_user`` vroeg de schrijfscope niet op, dus
kon een agent ``user_id`` zetten op elk record dat binnen zijn
*lees*scope viel. Alleen het plannen van een activiteit ging langs de
guard.

Het tweede deel is niet ingevoerd. In de productiedatabase (13-8-2026,
alleen gelezen) hebben alle 24 open tickets — ``close_date = False`` en
``stage_id.fold = False`` — een lege ``sla_deadline``; geen enkel open
ticket heeft er wél een. Een harde weigering op "geen SLA-deadline" zou
dus honderd procent van het actuele open ticketwerk blokkeren. De test
hieronder legt dat vast, zodat die regel niet later stilletzwijgend
terugkomt.
"""
from odoo.tests import common, tagged


@tagged("post_install", "-at_install", "daadit_ai")
class TestAssignUserWriteScope(common.TransactionCase):

    def setUp(self):
        super().setUp()
        if "helpdesk.ticket" not in self.env:
            self.skipTest("helpdesk.ticket is hier niet beschikbaar")
        self.Ticket = self.env["helpdesk.ticket"]
        self.Stage = self.env["helpdesk.stage"]
        self.agent = self.env["ai.agent"].create({"name": "Hilda (test)"})
        # Exact het domein dat Hilda in productie heeft.
        self.env["daadit.ai.agent.activity.scope"].create({
            "agent_id": self.agent.id,
            "model_name": "helpdesk.ticket",
            "record_domain": (
                '[["close_date", "=", false], '
                '["stage_id.fold", "=", false]]'
            ),
        })
        self.open_stage = self.Stage.create({
            "name": "Assigned (test)", "fold": False, "sequence": 10,
        })
        self.folded_stage = self.Stage.create({
            "name": "Solved (test)", "fold": True, "sequence": 90,
        })
        self.assignee = self.env["res.users"].create({
            "name": "Behandelaar (test)",
            "login": "behandelaar-1079-test",
        })

    def _assign(self, ticket):
        return self.agent._ai_tool_assign_user(
            model_name="helpdesk.ticket",
            record_id=ticket.id,
            user_id=self.assignee.id,
        )

    def test_an_open_ticket_without_sla_deadline_may_still_be_assigned(self):
        """Het tegenvoorbeeld: 24 van 24 open tickets in productie hebben
        geen SLA-deadline. Weigeren op dat veld blokkeert echt werk."""
        ticket = self.Ticket.create({
            "name": "Open ticket zonder SLA (test)",
            "stage_id": self.open_stage.id,
        })
        self.assertFalse(
            ticket.sla_deadline,
            "Deze test gaat juist over een ticket zónder SLA-deadline",
        )
        out = self._assign(ticket)
        self.assertTrue(out.get("ok"), out)
        self.assertNotIn("blocked_by_scope_guard", out)
        self.assertEqual(ticket.user_id, self.assignee)

    def test_a_folded_stage_is_refused_before_the_write(self):
        """Het misbruikgeval: de weigering moet vallen vóór de write en
        het ticket mag niet zijn aangeraakt."""
        ticket = self.Ticket.create({
            "name": "Afgehandeld ticket (test)",
            "stage_id": self.folded_stage.id,
        })
        before = ticket.user_id
        out = self._assign(ticket)
        self.assertFalse(out.get("ok"))
        self.assertFalse(out.get("written"))
        self.assertTrue(out.get("blocked_by_scope_guard"))
        self.assertEqual(
            ticket.user_id, before,
            "Een geweigerde toewijzing mag niets hebben geschreven",
        )

    def test_a_refusal_names_the_condition_that_failed(self):
        """Een blinde weigering laat een agent hetzelfde nog twee keer
        proberen; hij moet kunnen zien wélke voorwaarde hem tegenhoudt."""
        ticket = self.Ticket.create({
            "name": "Afgehandeld ticket (test)",
            "stage_id": self.folded_stage.id,
        })
        reason = self._assign(ticket)["error"]
        self.assertIn("SCOPE-GUARD", reason)
        self.assertIn("stage_id.fold", reason)
        self.assertNotIn("sla_deadline", reason)

    def test_a_model_outside_the_write_scope_is_refused_with_alternatives(self):
        if "crm.lead" not in self.env:
            self.skipTest("crm.lead is hier niet beschikbaar")
        lead = self.env["crm.lead"].create({"name": "Lead (test)"})
        before = lead.user_id
        out = self.agent._ai_tool_assign_user(
            model_name="crm.lead",
            record_id=lead.id,
            user_id=self.assignee.id,
        )
        self.assertTrue(out.get("blocked_by_scope_guard"))
        self.assertIn("helpdesk.ticket", out["error"])
        self.assertEqual(lead.user_id, before)

    def test_an_agent_without_scope_lines_may_not_assign_anything(self):
        """Default DICHT: zonder scoperegel geen schrijfactie."""
        naked = self.env["ai.agent"].create({"name": "Zonder scope (test)"})
        ticket = self.Ticket.create({
            "name": "Open ticket (test)", "stage_id": self.open_stage.id,
        })
        out = naked._ai_tool_assign_user(
            model_name="helpdesk.ticket",
            record_id=ticket.id,
            user_id=self.assignee.id,
        )
        self.assertTrue(out.get("blocked_by_scope_guard"))
        self.assertIn("geen schrijfscope", out["error"])
        self.assertFalse(ticket.user_id)
