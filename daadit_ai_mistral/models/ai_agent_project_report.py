# -*- coding: utf-8 -*-
"""Projectrapportage met een vaste vorm: doel, fasen, planning, voortgang.

Waarom dit bestaat. De projectmanager-collega kon taken aanmaken en
bijwerken, maar het antwoord op de enige vraag die een opdrachtgever
stelt — *wat is het doel, welke fasen liggen ervoor en waar staan we* —
leefde nergens vast. Elke ronde schreef hij een nieuw vrij verhaal in de
chat: andere indeling, andere getallen, niets terug te vinden in Odoo.

Deze drie tools leggen die vorm vast in records in plaats van in een
prompt:

* ``project_goal_set`` zet het doel in de projectbeschrijving, in een
  eigen gemarkeerd blok zodat de rest van een zorgvuldig opgebouwde
  beschrijving blijft staan;
* ``project_phase_upsert`` legt één fase vast als ``project.milestone``
  — met een datum, want een fase zonder datum is geen planning;
* ``project_report`` schrijft de voortgang als ``project.update``, en
  weigert dat zolang doel of fasen ontbreken.

De getallen in die rapportage komen uit de database en niet uit het
taalmodel: fasen, aantallen taken, afgeronde taken en uren worden hier
geteld. Het model levert alleen wat het écht toevoegt (status, risico's,
volgende stap). Zo kan een rapportage niet meer voortgang melden dan er
in Odoo staat.

De schrijfgrens is dezelfde als voor de andere schrijftools: de
scoperecords uit ``daadit.ai.agent.activity.scope`` op
``project.project``. Wie daar geen regel heeft, rapporteert niets.
"""
import logging
import re
from datetime import date

from markupsafe import Markup, escape

from odoo import _, models
from odoo.tools import html2plaintext
from odoo.tools.misc import format_date

_logger = logging.getLogger(__name__)

# Het doelblok in de projectbeschrijving. Alles buiten deze markers
# blijft ongemoeid: de beschrijving van een implementatieproject bevat
# rolverdeling, cadans en succesfactoren die niemand door een agent
# overschreven wil zien. De marker is een class en geen data-attribuut:
# de beschrijving is een gesanitiseerd html-veld en Odoo's sanitizer
# gooit onbekende data-* attributen weg, waardoor het blok na het
# wegschrijven niet meer te vinden was.
GOAL_MARKER = "o_daadit_goal"
GOAL_BLOCK_RE = re.compile(
    r'<div[^>]*class="[^"]*\bo_daadit_goal\b[^"]*"[^>]*>.*?</div>',
    re.IGNORECASE | re.DOTALL,
)

# Kort en bondig, maar wel een doel. Onder de ondergrens staat er iets
# als "Odoo uitrollen" — dat is een opdracht, geen doel; boven de
# bovengrens is het geen doel meer maar een projectplan.
GOAL_MIN_CHARS = 60
GOAL_MAX_CHARS = 700

# Vrije tekst van het model in de rapportage (risico's, volgende stap).
FREE_TEXT_MAX_CHARS = 1200

# De statussen die Odoo zelf op een project-update kent.
STATUS_VALUES = ("on_track", "at_risk", "off_track", "on_hold", "done")

# Taakstatussen die als afgerond gelden (Odoo 19).
CLOSED_TASK_STATES = ("1_done", "1_canceled")

WRITE_MODEL = "project.project"


class AIAgent(models.Model):
    _inherit = "ai.agent"

    # ------------------------------------------------------------------
    # Gedeelde controles
    # ------------------------------------------------------------------
    def _daadit_project_target(self, project_id):
        """``(project, error)`` — het project, of de weigering ervoor."""
        self.ensure_one()
        try:
            pid = int(project_id or 0)
        except (TypeError, ValueError):
            pid = 0
        if not pid:
            return None, {"ok": False, "error": _(
                "Geef project_id mee (het id van het project in Odoo).")}
        project = self.env["project.project"].sudo().browse(pid).exists()
        if not project:
            return None, {"ok": False, "error": _(
                "Project #%s bestaat niet. Zoek het project eerst op.",
            ) % pid}
        allowed, reason = self._daadit_write_scope(WRITE_MODEL, pid)
        if not allowed:
            _logger.warning(
                "SCOPE-GUARD blokkeerde projectrapportage: agent %s -> "
                "project #%s", self.id, pid)
            return None, {
                "ok": False,
                "written": False,
                "blocked_by_scope_guard": True,
                "error": reason,
            }
        return project, None

    @staticmethod
    def _daadit_clean_free_text(value):
        """Vrije tekst van het model: platte tekst, ingekort, of leeg."""
        text = html2plaintext(value) if "<" in (value or "") else (value or "")
        text = re.sub(r"[ \t]+", " ", text).strip()
        return text[:FREE_TEXT_MAX_CHARS]

    def _daadit_project_goal(self, project):
        """Het doel van dit project als platte tekst, of ``''``."""
        block = GOAL_BLOCK_RE.search(project.description or "")
        if not block:
            return ""
        return re.sub(r"\s+", " ", html2plaintext(block.group(0))).strip()

    def _daadit_project_phases(self, project):
        """De fasen van dit project, met de cijfers erbij geteld.

        Eén regel per ``project.milestone``, in de volgorde waarin ze
        gepland staan. ``task_count`` en ``done_task_count`` komen uit
        Odoo's eigen berekening op de milestone.
        """
        milestones = project.sudo().milestone_ids.sorted(
            key=lambda m: (m.deadline or date.max, m.sequence, m.id))
        today = date.today()
        phases = []
        for milestone in milestones:
            total = milestone.task_count
            done = milestone.done_task_count
            phases.append({
                "id": milestone.id,
                "name": milestone.name,
                "deadline": milestone.deadline,
                "reached": bool(milestone.is_reached),
                "overdue": bool(
                    milestone.deadline
                    and not milestone.is_reached
                    and milestone.deadline < today
                ),
                "task_count": total,
                "done_task_count": done,
                "percent": int(round(100.0 * done / total)) if total else 0,
            })
        return phases

    def _daadit_project_figures(self, project):
        """Taken en uren van het project, geteld in de database."""
        Task = self.env["project.task"].sudo()
        tasks = Task.search([("project_id", "=", project.id)])
        closed = tasks.filtered(lambda t: t.state in CLOSED_TASK_STATES)
        allocated = sum(tasks.mapped("allocated_hours") or [0.0])
        spent = sum(tasks.mapped("effective_hours") or [0.0])
        total = len(tasks)
        return {
            "task_count": total,
            "closed_task_count": len(closed),
            "open_task_count": total - len(closed),
            "percent_done": (
                int(round(100.0 * len(closed) / total)) if total else 0),
            "allocated_hours": round(allocated, 1),
            "spent_hours": round(spent, 1),
        }

    # ------------------------------------------------------------------
    # Tool 1 — het doel
    # ------------------------------------------------------------------
    def _ai_tool_project_goal_set(self, project_id=None, goal=None, **_extra):
        """Zet het doel van een project in de projectbeschrijving."""
        project, error = self._daadit_project_target(project_id)
        if error:
            return error
        text = self._daadit_clean_free_text(goal)
        if len(text) < GOAL_MIN_CHARS:
            return {"ok": False, "written": False, "error": _(
                "Dit is te kort voor een doel (%(len)s tekens, minimaal "
                "%(min)s). Noem wat er af is en waaraan je dat ziet, niet "
                "alleen de opdracht.",
                len=len(text), min=GOAL_MIN_CHARS,
            )}
        if len(text) > GOAL_MAX_CHARS:
            return {"ok": False, "written": False, "error": _(
                "Dit doel is te lang (%(len)s tekens, maximaal %(max)s). "
                "Kort en bondig: het doel zelf, niet het plan eromheen. "
                "Fasen leg je vast met de fase-tool.",
                len=len(text), max=GOAL_MAX_CHARS,
            )}

        block = Markup(
            '<div class="o_daadit_goal"><h3>Doel</h3><p>%s</p></div>'
        ) % text
        previous = self._daadit_project_goal(project)
        # Als platte tekst optellen bij de beschrijving: die is Markup, en
        # `str + Markup` escapet de linkerkant, waardoor het doelblok als
        # letterlijke html-tekst in het veld belandt.
        description = str(project.description or "")
        if GOAL_BLOCK_RE.search(description):
            new_description = GOAL_BLOCK_RE.sub(
                lambda _m: str(block), description, count=1)
        else:
            new_description = str(block) + description
        if new_description == description:
            return {
                "ok": False, "written": False, "skipped": True,
                "reason": "unchanged", "project_id": project.id,
                "goal": text,
            }
        project.sudo().write({"description": new_description})
        _logger.info(
            "Projectdoel vastgelegd door agent %s op project #%s",
            self.name, project.id)
        return {
            "ok": True,
            "written": True,
            "project_id": project.id,
            "project": project.name,
            "goal": text,
            "previous_goal": previous or None,
            "note": _(
                "Het doel staat in de projectbeschrijving. De rest van de "
                "beschrijving is niet aangeraakt."),
        }

    # ------------------------------------------------------------------
    # Tool 2 — de fasen
    # ------------------------------------------------------------------
    def _ai_tool_project_phase_upsert(self, project_id=None, name=None,
                                      deadline=None, sequence=None,
                                      reached=None, **_extra):
        """Leg één fase van een project vast als milestone met datum."""
        project, error = self._daadit_project_target(project_id)
        if error:
            return error
        label = re.sub(r"\s+", " ", (name or "")).strip()
        if not label:
            return {"ok": False, "written": False, "error": _(
                "Geef een naam voor de fase, bijvoorbeeld 'Inrichting "
                "en bouw'.")}
        parsed_deadline, deadline_error = self._daadit_parse_phase_deadline(
            deadline)
        if deadline_error:
            return deadline_error

        Milestone = self.env["project.milestone"].sudo()
        existing = Milestone.search([
            ("project_id", "=", project.id),
            ("name", "=ilike", label),
        ], limit=1)
        values = {"deadline": parsed_deadline}
        if sequence is not None:
            try:
                values["sequence"] = int(sequence)
            except (TypeError, ValueError):
                return {"ok": False, "written": False, "error": _(
                    "sequence moet een geheel getal zijn.")}
        if reached is not None:
            values["is_reached"] = bool(reached)

        enabled_milestones = False
        if not project.allow_milestones:
            # Zonder deze schakelaar bestaan de fasen wel, maar ziet
            # niemand ze terug in het project.
            project.sudo().write({"allow_milestones": True})
            enabled_milestones = True

        if existing:
            before = {
                "deadline": existing.deadline,
                "is_reached": existing.is_reached,
                "sequence": existing.sequence,
            }
            changed = {
                key: value for key, value in values.items()
                if before.get(key) != value
            }
            if not changed and not enabled_milestones:
                return {
                    "ok": False, "written": False, "skipped": True,
                    "reason": "unchanged", "phase_id": existing.id,
                    "phase": existing.name,
                    "message": _(
                        "Deze fase staat er al zo in. Er is niets gewijzigd."),
                }
            if changed:
                existing.write(changed)
            milestone = existing
            created = False
        else:
            milestone = Milestone.create(dict(
                values, name=label, project_id=project.id))
            created = True
        _logger.info(
            "Projectfase %s door agent %s op project #%s (%s)",
            "aangemaakt" if created else "bijgewerkt",
            self.name, project.id, label)
        return {
            "ok": True,
            "written": True,
            "created": created,
            "project_id": project.id,
            "project": project.name,
            "phase_id": milestone.id,
            "phase": milestone.name,
            "deadline": str(milestone.deadline),
            "reached": bool(milestone.is_reached),
            "milestones_enabled": enabled_milestones,
        }

    def _daadit_parse_phase_deadline(self, deadline):
        """``(date, error)`` — de fasedatum, of de weigering ervoor."""
        raw = (deadline or "").strip() if isinstance(deadline, str) else deadline
        if not raw:
            return None, {"ok": False, "written": False, "error": _(
                "Geef een einddatum voor deze fase als JJJJ-MM-DD. Een "
                "fase zonder datum levert geen planning op.")}
        if isinstance(raw, date):
            return raw, None
        try:
            return date.fromisoformat(raw[:10]), None
        except ValueError:
            return None, {"ok": False, "written": False, "error": _(
                "'%s' is geen datum die ik kan lezen. Gebruik JJJJ-MM-DD.",
            ) % raw}

    # ------------------------------------------------------------------
    # Tool 3 — de voortgang
    # ------------------------------------------------------------------
    def _ai_tool_project_report(self, project_id=None, status=None,
                                risks=None, next_step=None, **_extra):
        """Schrijf de voortgangsrapportage van een project weg.

        Weigert zolang het doel of de fasen ontbreken, en noemt dan wat
        er mist: een rapportage zonder die twee is een verhaal zonder
        maatstaf.
        """
        project, error = self._daadit_project_target(project_id)
        if error:
            return error
        if status not in STATUS_VALUES:
            return {"ok": False, "written": False, "error": _(
                "status moet één van deze zijn: %s.",
            ) % ", ".join(STATUS_VALUES)}

        goal = self._daadit_project_goal(project)
        phases = self._daadit_project_phases(project)
        missing = []
        if len(goal) < GOAL_MIN_CHARS:
            missing.append(_(
                "het doel van dit project (leg het eerst vast met de "
                "doel-tool)"))
        if not phases:
            missing.append(_(
                "de fasen naar dat doel (leg elke fase met datum vast met "
                "de fase-tool)"))
        undated = [p["name"] for p in phases if not p["deadline"]]
        if undated:
            missing.append(_(
                "een einddatum bij de fasen: %s",
            ) % ", ".join(undated))
        if missing:
            return {
                "ok": False,
                "written": False,
                "error": _(
                    "Nog geen rapportage geschreven, want dit ontbreekt: "
                    "%s.",
                ) % "; ".join(missing),
                "missing": missing,
                "project_id": project.id,
            }

        figures = self._daadit_project_figures(project)
        body = self._daadit_report_body(
            project, goal, phases, figures,
            self._daadit_clean_free_text(risks),
            self._daadit_clean_free_text(next_step),
        )
        today = date.today()
        title = _("Voortgang %(project)s — %(date)s", project=project.name,
                  date=format_date(self.env, today))
        values = {
            "name": title,
            "status": status,
            "progress": figures["percent_done"],
            "description": body,
            "date": today,
            "project_id": project.id,
        }
        Update = self.env["project.update"].sudo()
        existing = Update.search([
            ("project_id", "=", project.id), ("date", "=", today),
        ], limit=1)
        if existing:
            # Twee rapportages op één dag maken de reeks onleesbaar; de
            # nieuwste inzichten horen in die van vandaag.
            existing.write(values)
            update = existing
        else:
            update = Update.create(values)
        _logger.info(
            "Projectrapportage %s door agent %s op project #%s",
            "bijgewerkt" if existing else "geschreven", self.name, project.id)
        return {
            "ok": True,
            "written": True,
            "replaced_today": bool(existing),
            "update_id": update.id,
            "project_id": project.id,
            "project": project.name,
            "status": status,
            "progress": figures["percent_done"],
            "phases": [{
                "phase": p["name"],
                "deadline": str(p["deadline"]),
                "reached": p["reached"],
                "overdue": p["overdue"],
                "tasks": p["task_count"],
                "tasks_done": p["done_task_count"],
                "percent": p["percent"],
            } for p in phases],
            "figures": figures,
            "note": _(
                "De cijfers in deze rapportage zijn geteld in Odoo. Noem "
                "in je antwoord alleen deze getallen."),
        }

    def _daadit_report_body(self, project, goal, phases, figures,
                            risks, next_step):
        """De rapportage zelf: altijd dezelfde vier kopjes, in deze orde."""
        rows = Markup("")
        for phase in phases:
            if phase["reached"]:
                state = _("gehaald")
            elif phase["overdue"]:
                state = _("over datum")
            else:
                state = _("loopt")
            rows += Markup(
                "<tr><td>%(name)s</td><td>%(deadline)s</td>"
                "<td>%(state)s</td><td>%(done)s/%(total)s (%(pct)s%%)</td>"
                "</tr>"
            ) % {
                "name": phase["name"],
                "deadline": format_date(self.env, phase["deadline"]),
                "state": state,
                "done": phase["done_task_count"],
                "total": phase["task_count"],
                "pct": phase["percent"],
            }
        body = Markup(
            "<h3>%(goal_title)s</h3><p>%(goal)s</p>"
            "<h3>%(phase_title)s</h3>"
            "<table><thead><tr><th>%(h_phase)s</th><th>%(h_date)s</th>"
            "<th>%(h_state)s</th><th>%(h_tasks)s</th></tr></thead>"
            "<tbody>%(rows)s</tbody></table>"
            "<h3>%(progress_title)s</h3><ul>"
            "<li>%(tasks_line)s</li><li>%(hours_line)s</li></ul>"
        ) % {
            "goal_title": _("Doel"),
            "goal": goal,
            "phase_title": _("Fasen en planning"),
            "h_phase": _("Fase"),
            "h_date": _("Einddatum"),
            "h_state": _("Stand"),
            "h_tasks": _("Taken af"),
            "rows": rows,
            "progress_title": _("Voortgang"),
            "tasks_line": _(
                "%(done)s van %(total)s taken af (%(pct)s%%), "
                "%(open)s nog open",
                done=figures["closed_task_count"],
                total=figures["task_count"],
                pct=figures["percent_done"],
                open=figures["open_task_count"],
            ),
            "hours_line": _(
                "%(spent)s van %(allocated)s begrote uren geschreven",
                spent=figures["spent_hours"],
                allocated=figures["allocated_hours"],
            ),
        }
        if risks:
            body += Markup("<h3>%s</h3><p>%s</p>") % (
                _("Risico's"), escape(risks))
        if next_step:
            body += Markup("<h3>%s</h3><p>%s</p>") % (
                _("Volgende stap"), escape(next_step))
        return str(body)
