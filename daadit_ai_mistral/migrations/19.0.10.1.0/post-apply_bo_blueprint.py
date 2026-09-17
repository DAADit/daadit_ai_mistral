# -*- coding: utf-8 -*-
"""Zet de blauwdruk van Bo (services/bo_blueprint) op het catalogusrecord.

Eerste keer dat Bo's opdracht, modellen, skills en activiteitscopes uit
code komen in plaats van uit de database. Additief voor toegang, de
opdracht wordt vervangen; zie ``ai.agent._daadit_apply_bo_blueprint``.
De nieuwe skills uit agent_skill_data.xml bestaan op dit punt al (data
gaat vóór post-migraties). Een database zonder Bo merkt hier niets van.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["ai.agent"]._daadit_seed_skills()
    env["ai.agent"]._daadit_apply_bo_blueprint()
