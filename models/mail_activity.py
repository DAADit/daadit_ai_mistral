# -*- coding: utf-8 -*-
"""Marker field so agent-created activities are countable.

The Fase-0 throttle (max N agent-activities per user per day,
Governance & guardrails knowledge id 173) needs to count exactly the
activities that agents created — not the ones humans made. A boolean
set by ``ai.agent._ai_tool_schedule_activity`` keeps that count precise
without heuristics on ``create_uid``.
"""
from odoo import fields, models


class MailActivity(models.Model):
    _inherit = "mail.activity"

    daadit_agent_created = fields.Boolean(
        string="Created by AI agent",
        default=False,
        index=True,
        help="Set when this activity was scheduled by an AI agent tool "
             "call. Used by the per-user daily throttle.",
    )
