# -*- coding: utf-8 -*-
"""Koppel de eerste product-skills aan bestaande DAADit agents.

De skills zelf komen uit ``data/agent_skill_data.xml``. Deze migratie
vult alleen de many2many op bekende agentnamen, en doet dat additief:
handmatig gekozen skills blijven staan.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["ai.agent"]._daadit_seed_skills()
