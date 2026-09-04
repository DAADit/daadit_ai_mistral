# -*- coding: utf-8 -*-
"""De projectrapportage moet in records staan, niet in een verhaal.

Vier eigenschappen die deze tools moeten hebben, en die geen prompt kan
garanderen:

* een beschrijving die iemand met zorg heeft opgebouwd blijft staan als
  het doel erin wordt gezet;
* een fase zonder datum wordt geweigerd — anders is er geen planning;
* een rapportage zonder doel of fasen wordt geweigerd, mét de mededeling
  wat er mist, zodat het model zich kan herstellen;
* de voortgang in de rapportage komt uit de database, ook als het model
  iets anders beweert.
"""
from datetime import date, timedelta

from odoo.tests import common, tagged


@tagged("post_install", "-at_install", "daadit_ai")
class TestProjectReportTools(common.TransactionCase):

    def setUp(self):
        super().setUp()
        if "project.update" not in self.env:
            self.skipTest("project.update is hier niet beschikbaar")
        self.project = self.env["project.project"].create({
            "name": "Implementatie (test)",
            "description": "<p>Rolverdeling: Nick stuurt, klant beslist.</p>",
            "allow_milestones": False,
        })
        self.agent = self.env["ai.agent"].create({"name": "Pim (test)"})
        self.env["daadit.ai.agent.activity.scope"].create({
            "agent_id": self.agent.id,
            "model_name": "project.project",
            "record_domain": "[]",
        })
        self.goal = (
            "Het projectteam draait per 1 december volledig op Odoo, met "
            "de facturatie in eigen beheer en zonder terugval op Excel."
        )
        self.deadline = date.today() + timedelta(days=30)

    def _set_goal(self, goal=None):
        return self.agent._ai_tool_project_goal_set(
            project_id=self.project.id, goal=goal or self.goal)

    def _add_phase(self, name, deadline=None, **kwargs):
        return self.agent._ai_tool_project_phase_upsert(
            project_id=self.project.id, name=name,
            deadline=deadline if deadline is not None else str(self.deadline),
            **kwargs)

    def _report(self, **kwargs):
        return self.agent._ai_tool_project_report(
            project_id=self.project.id,
            status=kwargs.pop("status", "on_track"), **kwargs)

    # -- het doel ------------------------------------------------------
    def test_the_existing_description_survives_setting_the_goal(self):
        """Een beschrijving met rolverdeling en cadans mag een agent niet
        overschrijven — het doel komt in een eigen blok erbovenop."""
        out = self._set_goal()
        self.assertTrue(out.get("ok"), out)
        self.assertIn("Rolverdeling", self.project.description)
        self.assertIn("Odoo", self.project.description)

    def test_the_goal_block_lands_as_html_and_not_as_text(self):
        """Twee manieren waarop het blok stil sneuvelde in een veld dat al
        een beschrijving had: de sanitizer gooide een eigen data-attribuut
        weg, en `str + Markup` escapete het blok tot letterlijke tekst.
        Beide keren bleef het doel onvindbaar en kwam er bij de volgende
        ronde een tweede doel bovenop."""
        self._set_goal()
        self.assertIn('<div class="o_daadit_goal">', self.project.description)
        self.assertNotIn("&lt;div", self.project.description)
        read_back = self.agent._daadit_project_goal(self.project)
        self.assertIn(self.goal, read_back)
        self.assertNotIn("Rolverdeling", read_back)

    def test_a_description_without_a_goal_block_has_no_goal(self):
        """Anders zou de hele beschrijving als doel worden gerapporteerd."""
        self.assertEqual(
            self.agent._daadit_project_goal(self.project), "")

    def test_setting_the_goal_twice_replaces_only_the_goal_block(self):
        self._set_goal()
        second = "Per 1 maart factureert de klant zelf, binnen twee dagen "
        second += "na levering, zonder tussenkomst van ons team."
        out = self._set_goal(second)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(self.project.description.count("o_daadit_goal"), 1)
        self.assertIn("Rolverdeling", self.project.description)
        self.assertNotIn("volledig op Odoo", self.project.description)

    def test_a_one_line_assignment_is_not_a_goal(self):
        out = self._set_goal("Odoo uitrollen")
        self.assertFalse(out.get("ok"))
        self.assertFalse(out.get("written"))
        self.assertIn("te kort", out["error"])
        self.assertNotIn("o_daadit_goal", self.project.description or "")

    # -- de fasen ------------------------------------------------------
    def test_a_phase_without_a_date_is_refused(self):
        out = self._add_phase("Inrichting en bouw", deadline="")
        self.assertFalse(out.get("ok"))
        self.assertIn("JJJJ-MM-DD", out["error"])
        self.assertFalse(self.project.milestone_ids)

    def test_a_phase_is_created_and_made_visible_on_the_project(self):
        out = self._add_phase("Inrichting en bouw")
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("created"))
        self.assertTrue(
            self.project.allow_milestones,
            "Zonder deze schakelaar ziet niemand de fasen terug",
        )
        self.assertEqual(len(self.project.milestone_ids), 1)

    def test_the_same_phase_name_updates_instead_of_duplicating(self):
        self._add_phase("Inrichting en bouw")
        later = str(self.deadline + timedelta(days=14))
        out = self._add_phase("inrichting en bouw", deadline=later)
        self.assertTrue(out.get("ok"), out)
        self.assertFalse(out.get("created"))
        self.assertEqual(len(self.project.milestone_ids), 1)
        self.assertEqual(str(self.project.milestone_ids.deadline), later)

    # -- de voortgang --------------------------------------------------
    def test_a_report_without_goal_or_phases_names_what_is_missing(self):
        out = self._report()
        self.assertFalse(out.get("ok"))
        self.assertFalse(out.get("written"))
        self.assertEqual(len(out["missing"]), 2)
        self.assertIn("doel", out["error"])
        self.assertIn("fasen", out["error"])
        self.assertFalse(self.env["project.update"].search(
            [("project_id", "=", self.project.id)]))

    def test_the_progress_comes_from_the_database(self):
        self._set_goal()
        self._add_phase("Inrichting en bouw")
        Task = self.env["project.task"]
        for index in range(3):
            Task.create({
                "name": "Taak %s (test)" % index,
                "project_id": self.project.id,
                "state": "1_done" if index < 2 else "01_in_progress",
            })
        out = self._report(risks="Testdata ontbreekt bij de klant.")
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out["progress"], 67)
        self.assertEqual(out["figures"]["closed_task_count"], 2)
        update = self.env["project.update"].browse(out["update_id"])
        self.assertEqual(update.progress, 67)
        self.assertIn("Testdata ontbreekt", update.description)
        for heading in ("Doel", "Fasen en planning", "Voortgang"):
            self.assertIn(heading, update.description)

    def test_two_reports_on_one_day_leave_one_record(self):
        self._set_goal()
        self._add_phase("Inrichting en bouw")
        first = self._report()
        second = self._report(status="at_risk")
        self.assertEqual(first["update_id"], second["update_id"])
        self.assertTrue(second["replaced_today"])
        self.assertEqual(
            self.env["project.update"].search_count(
                [("project_id", "=", self.project.id)]), 1)
        self.assertEqual(
            self.env["project.update"].browse(
                second["update_id"]).status, "at_risk")

    def test_an_unknown_status_is_refused(self):
        self._set_goal()
        self._add_phase("Inrichting en bouw")
        out = self._report(status="gaat_wel_lekker")
        self.assertFalse(out.get("ok"))
        self.assertIn("on_track", out["error"])

    # -- de schrijfgrens -----------------------------------------------
    def test_an_agent_without_a_scope_line_writes_nothing(self):
        naked = self.env["ai.agent"].create({"name": "Zonder scope (test)"})
        calls = [
            naked._ai_tool_project_goal_set(
                project_id=self.project.id, goal=self.goal),
            naked._ai_tool_project_phase_upsert(
                project_id=self.project.id, name="Fase",
                deadline=str(self.deadline)),
            naked._ai_tool_project_report(
                project_id=self.project.id, status="on_track"),
        ]
        for out in calls:
            self.assertTrue(out.get("blocked_by_scope_guard"), out)
            self.assertFalse(out.get("written"))
        self.assertNotIn("o_daadit_goal", self.project.description or "")
        self.assertFalse(self.project.milestone_ids)

    def test_a_project_outside_the_scope_domain_is_refused(self):
        other = self.env["project.project"].create({"name": "Ander (test)"})
        narrow = self.env["ai.agent"].create({"name": "Beperkte Pim (test)"})
        self.env["daadit.ai.agent.activity.scope"].create({
            "agent_id": narrow.id,
            "model_name": "project.project",
            "record_domain": '[["id", "=", %s]]' % self.project.id,
        })
        out = narrow._ai_tool_project_phase_upsert(
            project_id=other.id, name="Fase", deadline=str(self.deadline))
        self.assertTrue(out.get("blocked_by_scope_guard"))
        self.assertFalse(other.milestone_ids)
