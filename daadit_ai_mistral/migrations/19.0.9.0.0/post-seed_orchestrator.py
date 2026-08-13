# -*- coding: utf-8 -*-
"""Mark Robin / Ask AI as orchestrator and attach the handoff tool.

Delegates to ``ai.agent._daadit_seed_orchestrator`` (idempotent,
non-widening). A database without those agents or tools notices nothing.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["ai.agent"]._daadit_seed_orchestrator()
