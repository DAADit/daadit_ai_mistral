# -*- coding: utf-8 -*-
"""Hang de persona-records om naar ``daadit_ai_personas`` (de split).

Het skillmodel, de 21 skillrecords, de views/menu's en het
``daadit_skill_ids``-veld verhuizen per v19.0.11.0.0 naar het nieuwe
module ``daadit_ai_personas``. Zonder deze omhanging zou de upgrade van
dít module (dat die data-bestanden niet meer declareert) de records als
wees opruimen — en daarmee de skillskoppeling van elke agent weggooien.

Dit draait als PRE-migratie: vóór Odoo's wezenopruiming. Records die
``daadit_ai_personas`` al bezit (vers geïnstalleerde database) worden
overgeslagen — de unieke sleutel (module, name) blijft intact.

Een database die ``daadit_ai_personas`` daarna nooit installeert houdt
de omgehangen records als inerte data (model niet geregistreerd, geen
views). Installatie van ``daadit_ai_personas`` adopteert ze naadloos,
omdat de xml-id-namen daar ongewijzigd terugkomen.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        r"""
        UPDATE ir_model_data imd
           SET module = 'daadit_ai_personas'
         WHERE imd.module = 'daadit_ai_mistral'
           AND (
                imd.name LIKE 'skill\_%' ESCAPE '\'
             OR imd.name LIKE '%daadit\_ai\_agent\_skill%' ESCAPE '\'
             OR imd.name IN (
                    'ai_agent_view_form_inherit_daadit_skills',
                    'ai_agent_view_search_inherit_daadit_skills',
                    'field_ai_agent__daadit_skill_ids'
                )
           )
           AND NOT EXISTS (
                SELECT 1 FROM ir_model_data d2
                 WHERE d2.module = 'daadit_ai_personas'
                   AND d2.name = imd.name
           )
        """
    )
    _logger.info(
        "daadit_ai_mistral 19.0.11.0.0: %d persona-records omgehangen "
        "naar daadit_ai_personas", cr.rowcount,
    )
