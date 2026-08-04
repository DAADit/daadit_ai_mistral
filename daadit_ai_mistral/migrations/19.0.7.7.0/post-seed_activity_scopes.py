# -*- coding: utf-8 -*-
"""Zet de vastgelegde activiteit-scoperegels neer bij de upgrade.

De regels zelf staan in ``SEED_ACTIVITY_SCOPES``; deze migratie maakt
ze aan voor de agents die nog geen enkele regel hebben. Dat is
niet-verruimend: bestaat er al een regel, dan blijft die staan.

De live serveractie blijft in deze versie ongewijzigd — hij wordt in een
aparte ronde naar :meth:`_daadit_activity_scope` gebracht, zodat de
grens per agent eerst te vergelijken en te controleren is voordat hij
de beslissing overneemt.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["ai.agent"]._daadit_seed_activity_scopes()
