# -*- coding: utf-8 -*-
"""Zet de blauwdruk van Bo (``services/bo_blueprint``) op het
catalogusrecord — bij installatie, bij upgrade en op aanvraag.

Additief waar het om toegang gaat: modellen, skills en activiteitscopes
komen erbij en gaan er hier nooit af, zodat een handmatige beperking op
de catalogus (een geblokkeerd model, een veld op de privacylijst) blijft
staan. Alleen de opdracht wordt vervangen, want juist die moet bij elke
publicatie dezelfde zijn. Eén keer per versie: de toegepaste versie
staat in ``ir.config_parameter``; ``force=True`` past hem opnieuw toe.
"""
import json
import logging

from odoo import api, models

from ..services import bo_blueprint

_logger = logging.getLogger(__name__)


class AIAgentBlueprint(models.Model):
    _inherit = "ai.agent"

    @api.model
    def _daadit_bo_catalog_agent(self):
        """Het catalogusrecord van Bo: op naam, en nooit een
        klantexemplaar (die hangen aan een inhuur)."""
        domain = [("name", "=", bo_blueprint.AGENT_NAME)]
        if "daadit_hire_id" in self._fields:
            domain.append(("daadit_hire_id", "=", False))
        return self.sudo().search(domain, order="id", limit=1)

    @api.model
    def _daadit_bo_blueprint_applied_version(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(
            bo_blueprint.CONFIG_KEY, "0",
        )
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    @api.model
    def _daadit_apply_bo_blueprint(self, force=False):
        """Pas de blauwdruk toe als er een nieuwere versie is.

        Levert een dict met wat er is gebeurd (``applied``, ``prompt``,
        ``models``, ``skills``, ``scopes``, ``missing_models``), zodat
        een migratie of test het kan nalezen. Zonder Bo in deze
        database gebeurt er niets en staat dat in de log.
        """
        result = {
            "applied": False, "prompt": False, "models": [],
            "skills": [], "scopes": [], "missing_models": [],
            "fields": [],
        }
        applied = self._daadit_bo_blueprint_applied_version()
        if not force and applied >= bo_blueprint.BLUEPRINT_VERSION:
            return result
        agent = self._daadit_bo_catalog_agent()
        if not agent:
            _logger.info(
                "Bo-blauwdruk v%s: geen catalogusagent '%s' in deze "
                "database; niets toegepast",
                bo_blueprint.BLUEPRINT_VERSION, bo_blueprint.AGENT_NAME,
            )
            return result

        vals = {}
        wanted_prompt = bo_blueprint.SYSTEM_PROMPT.strip()
        if (agent.system_prompt or "").strip() != wanted_prompt:
            vals["system_prompt"] = wanted_prompt
            result["prompt"] = True

        IrModel = self.env["ir.model"].sudo()
        blocked = set(agent.daadit_blocked_model_ids.mapped("model"))
        known = set(agent.daadit_allowed_model_ids.mapped("model"))
        wanted = [
            name for name in bo_blueprint.ALLOWED_MODELS
            if name not in known and name not in blocked
        ]
        found = IrModel.search([("model", "in", wanted)]) if wanted else IrModel
        result["missing_models"] = sorted(
            set(wanted) - set(found.mapped("model")),
        )
        if found:
            vals["daadit_allowed_model_ids"] = [(4, m.id) for m in found]
            result["models"] = sorted(found.mapped("model"))

        have_fields = [
            entry.strip()
            for entry in (agent.daadit_field_blocklist or "").split(",")
            if entry.strip()
        ]
        new_fields = [
            entry for entry in bo_blueprint.FIELD_BLOCKLIST
            if entry not in have_fields
        ]
        if new_fields:
            vals["daadit_field_blocklist"] = ",".join(have_fields + new_fields)
            result["fields"] = new_fields

        skill_ids = []
        for xmlid in bo_blueprint.SKILL_XMLIDS:
            skill = self.env.ref(
                "daadit_ai_mistral.%s" % xmlid, raise_if_not_found=False,
            )
            if skill and skill.id not in agent.daadit_skill_ids.ids:
                skill_ids.append(skill.id)
                result["skills"].append(skill.code)
        if skill_ids:
            vals["daadit_skill_ids"] = [(4, sid) for sid in skill_ids]

        if vals:
            agent.write(vals)

        Scope = self.env["daadit.ai.agent.activity.scope"].sudo()
        have_scopes = set(agent.daadit_activity_scope_ids.mapped("model_name"))
        for model_name, domain in bo_blueprint.ACTIVITY_SCOPES:
            if model_name in have_scopes:
                continue
            if not IrModel.search([("model", "=", model_name)], limit=1):
                continue
            Scope.create({
                "agent_id": agent.id,
                "model_name": model_name,
                "record_domain": json.dumps(domain),
            })
            result["scopes"].append(model_name)

        self.env["ir.config_parameter"].sudo().set_param(
            bo_blueprint.CONFIG_KEY, str(bo_blueprint.BLUEPRINT_VERSION),
        )
        result["applied"] = True
        _logger.info(
            "Bo-blauwdruk v%s toegepast op ai.agent %s: prompt=%s, "
            "modellen+%s, skills+%s, scopes+%s, ontbrekend=%s",
            bo_blueprint.BLUEPRINT_VERSION, agent.id, result["prompt"],
            result["models"], result["skills"], result["scopes"],
            result["missing_models"],
        )
        return result
