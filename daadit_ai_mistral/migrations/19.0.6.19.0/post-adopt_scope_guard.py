# -*- coding: utf-8 -*-
"""Laat de serveractie de beslissing aan de scoperecords over.

Ronde 2 van taak 747. De serveractie achter de plan-activiteit-tool
bevatte de volledige schrijfgrens als Python in een databaseveld. Die
grens staat sinds 19.0.6.18.0 als records in de repo; hier neemt hij de
beslissing ook werkelijk over.

Twee voorzorgen, omdat dit de hardste governance-laag is:

* de oude code wordt eerst weggeschreven naar een parameter, zodat
  terugzetten één write is;
* alleen een actie die de oude guard herkenbaar bevat wordt aangeraakt.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

BACKUP_PARAM = "daadit_ai_mistral.scope_guard_backup_code"


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    from odoo.addons.daadit_ai_mistral.models.ai_agent_activity_scope import (
        SERVER_ACTION_CODE,
    )

    env["ai.agent"]._daadit_seed_activity_scopes()

    watchdog = env["ai.agent"].sudo().search([("name", "=", "Argus")], limit=1)
    if watchdog:
        watchdog.daadit_repair_channel = True

    actions = env["ir.actions.server"].sudo().search([
        ("state", "=", "code"),
        ("code", "like", "SCOPE-GUARD"),
        ("code", "like", "_ai_tool_schedule_activity"),
    ])
    # Een actie die al doorgeeft is niet interessant; alleen de oude,
    # zelf-beslissende versie wordt overgezet.
    actions = actions.filtered(
        lambda a: "_daadit_schedule_activity_guarded" not in (a.code or ""))
    if not actions:
        _logger.info("Scope-guard: geen serveractie om over te zetten.")
        return

    env["ir.config_parameter"].sudo().set_param(
        BACKUP_PARAM,
        "\n\n# ---- volgende actie ----\n\n".join(
            "# actie %s (%s)\n%s" % (a.id, a.name, a.code) for a in actions),
    )
    actions.write({"code": SERVER_ACTION_CODE})
    _logger.warning(
        "Scope-guard: serveractie(s) %s geven de beslissing nu door aan "
        "_daadit_schedule_activity_guarded; oude code staat in %s.",
        actions.ids, BACKUP_PARAM)
