# -*- coding: utf-8 -*-
"""P0 reliability: re-enable Argus repair channel; attach coverage tool.

Live had ``daadit_repair_channel=False`` on Argus even after the earlier
seed migration, so AUTO-APPLY proposals fell into the normal activity
cap (taak 773). Also attach the new assurance-coverage tool to Argus'
topics when present.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Agent = env["ai.agent"].sudo()
    argus = Agent.search([("name", "=", "Argus")], limit=1)
    if argus and not argus.daadit_repair_channel:
        argus.daadit_repair_channel = True

    tool = env.ref(
        "daadit_ai_mistral.ir_actions_server_assurance_coverage",
        raise_if_not_found=False,
    )
    if not tool or not argus:
        return
    for topic in argus.topic_ids:
        if tool not in topic.tool_ids:
            topic.write({"tool_ids": [(4, tool.id)]})
