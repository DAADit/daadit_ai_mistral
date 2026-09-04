# -*- coding: utf-8 -*-
"""Sanne laat na de handtekening niet meer los.

Orderverwerking hoort bij sales, dus haar werk loopt door tot de
levering. Nazorg blijft van Hilda en factureren van Marit. De persona
staat als tekst in de database, dus we vervangen precies de twee
zinnen die de oude grens beschrijven — staan ze er niet (meer), dan
laat deze migratie de prompt ongemoeid.
"""
from odoo import SUPERUSER_ID, api

REPLACEMENTS = (
    (
        "Ik ben Sanne, sales manager. Ik houd uw pijplijn in beweging, "
        "van lead tot handtekening.",
        "Ik ben Sanne, sales manager. Ik houd uw pijplijn in beweging, "
        "van lead tot levering.",
    ),
    (
        "- **Van lead tot handtekening** \u2014 Mijn werk stopt pas bij een "
        "handtekening \u2014 of bij een nette nee.",
        "- **Van lead tot levering** \u2014 Mijn werk stopt niet bij de "
        "handtekening: ik blijf op de order tot die geleverd is \u2014 of "
        "het wordt een nette nee.",
    ),
    (
        "- **Klanten helpen na de verkoop** \u2014 Zodra de handtekening "
        "staat laat ik bewust los, anders voelt niemand zich echt "
        "eigenaar van de nazorg. Verwijs door naar **Hilda**.",
        "- **Klantvragen na de levering** \u2014 De order en de levering "
        "houd ik zelf in de hand; gaat het over hulp of klachten daarna, "
        "dan is dat een ander vak. Verwijs door naar **Hilda**.\n"
        "- **Factureren** \u2014 Ik meld welke geleverde order klaar is om "
        "te factureren, maar de factuur zelf maak ik niet. Verwijs door "
        "naar **Marit**.",
    ),
)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    agent = env["ai.agent"].search([("name", "=", "Sanne")], limit=1)
    if not agent:
        return
    prompt = agent.sudo().system_prompt or ""
    updated = prompt
    for old, new in REPLACEMENTS:
        if old in updated:
            updated = updated.replace(old, new)
    if updated != prompt:
        agent.sudo().write({"system_prompt": updated})
