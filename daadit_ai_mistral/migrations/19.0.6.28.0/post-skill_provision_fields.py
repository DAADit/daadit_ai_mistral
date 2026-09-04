# -*- coding: utf-8 -*-
"""Vul skill provision-velden op bestaande noupdate-records (taak 844)."""
import logging

_logger = logging.getLogger(__name__)

_UPDATES = {
    "finance.overdue_receivables": {
        "activity_scope_models": "account.move",
        "blocked_models": "account.payment",
    },
    "service.ticket_triage": {
        "activity_scope_models": "helpdesk.ticket",
    },
}


def migrate(cr, version):
    try:
        from odoo import api, SUPERUSER_ID
        env = api.Environment(cr, SUPERUSER_ID, {})
    except Exception:  # noqa: BLE001
        _logger.exception("daadit_ai_mistral 19.0.6.28.0: env open failed")
        return
    Skill = env["daadit.ai.agent.skill"].sudo()
    for code, vals in _UPDATES.items():
        skill = Skill.search([("code", "=", code)], limit=1)
        if skill:
            skill.write(vals)
    _logger.info("daadit_ai_mistral 19.0.6.28.0: skill provision fields filled")
