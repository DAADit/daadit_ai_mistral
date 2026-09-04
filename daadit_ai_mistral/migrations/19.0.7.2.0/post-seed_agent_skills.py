# -*- coding: utf-8 -*-
"""Bo krijgt de hele boekhoudset in plaats van alleen grootboekcontrole.

Additief, net als de eerste seed: skills die een mens erbij koos blijven
staan, en een database zonder een agent 'Bo' slaat hem over.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["ai.agent"]._daadit_seed_skills()
