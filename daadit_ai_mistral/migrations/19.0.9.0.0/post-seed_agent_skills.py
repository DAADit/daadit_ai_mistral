# -*- coding: utf-8 -*-
"""Product-skills koppelen (historisch).

v19.0.11.0.0: de persona-seeds verhuisden naar ``daadit_ai_personas``.
Deze migratie draait alleen nog als dat module de methode(n) al op
``ai.agent`` heeft gezet (personas geinstalleerd); anders is er niets
te seeden en slaat hij stil over. Upgradepaden van voor de split
blijven zo werken zonder import van verdwenen code.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    agent = env["ai.agent"]
    for method in ['_daadit_seed_skills']:
        if hasattr(agent, method):
            getattr(agent, method)()
