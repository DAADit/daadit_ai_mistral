# -*- coding: utf-8 -*-
"""Zet de scoperegels van de financiële bezetting neer bij de upgrade.

De vier rollen onder Floris (Bo, Dirk, Fenna, Coen) staan sinds deze
versie in ``SEED_ACTIVITY_SCOPES``. Hetzelfde seed-pad als 19.0.6.18.0:
idempotent en niet-verruimend — een agent die al regels heeft wordt niet
aangeraakt, en een agent die in deze database niet bestaat wordt
overgeslagen. Een database zonder financiële agents merkt hier dus
niets van.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["ai.agent"]._daadit_seed_activity_scopes()
