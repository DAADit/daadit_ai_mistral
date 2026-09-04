# -*- coding: utf-8 -*-
"""Sanne krijgt de orderverwerkingsskills: van order tot levering.

Additief, net als de eerdere seeds: skills die een mens erbij koos
blijven staan, en een database zonder een agent 'Sanne' slaat hem over.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["ai.agent"]._daadit_seed_skills()
