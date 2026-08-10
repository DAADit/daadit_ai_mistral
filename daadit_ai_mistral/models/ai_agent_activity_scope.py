# -*- coding: utf-8 -*-
"""Hard per-agent scope for planned activities (governance, tool-laag).

Waarom dit bestaat. De schrijfgrens voor activiteiten — welke agent op
welk model een to-do mag plannen — stond tot nu toe als Python in het
``code``-veld van één serveractie in de database. Daar is hij niet
versiebeheerd, niet gereviewd, niet getest, en bij een nieuwe
klantdatabase bestaat hij niet. Dat is precies de laag die het strakst
zou moeten zitten.

Deze module zet die grens in records, zoals de harde leesscope dat al
doet: één regel per agent per model, met een optioneel record-domein.
De tool-laag vraagt hem op via :meth:`AIAgent._daadit_activity_scope`;
de prompt kan hem niet verruimen.

Twee dingen die de oude versie fout deed en hier niet meer kunnen:

* **Een bestemming toestaan die geen activiteit kán dragen.** Lux en Sem
  mochten ``website.page``, een model zonder ``mail.activity.mixin``.
  Elke poging kwam terug met een ORM-fout en hun voorstel was weg —
  drie mislukte tool-acties per dag. Een scope-regel op zo'n model
  weigert nu mét de uitleg en met de bestemmingen die wél werken.
* **Zwijgen over het alternatief.** Een weigering noemt nu de modellen
  die deze agent wél mag, zodat het model zichzelf kan herstellen in
  plaats van hetzelfde nog twee keer te proberen.

Default is DICHT: een agent zonder scoperegels mag niets.
"""
import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class AiAgentActivityScope(models.Model):
    _name = "daadit.ai.agent.activity.scope"
    _description = "AI Agent — Activity Write Scope"
    _order = "agent_id, model_name"

    agent_id = fields.Many2one(
        "ai.agent", string="AI Agent", required=True,
        ondelete="cascade", index=True,
    )
    # Geen ir.model-relatie: een scoperegel moet ook kunnen bestaan voor
    # een model dat in deze database (nog) niet geïnstalleerd is —
    # anders valt de hele governance-set om zodra één app ontbreekt.
    model_name = fields.Char(
        string="Model name", required=True, index=True,
        help="Technische modelnaam waarop deze agent een activiteit mag "
             "plannen, bijvoorbeeld knowledge.article.",
    )
    record_domain = fields.Char(
        string="Record domain", required=True, default="[]",
        help="Odoo-domein (JSON) waaraan het doelrecord moet voldoen. "
             "Leeg domein = elk record van dit model. Voorbeeld: "
             '[["root_article_id", "=", 191]]',
    )
    active = fields.Boolean(string="Active", default=True)

    _agent_model_uniq = models.Constraint(
        "UNIQUE(agent_id, model_name)",
        "Per agent bestaat er één scoperegel per model.",
    )

    @api.constrains("record_domain")
    def _check_record_domain(self):
        for rec in self:
            try:
                parsed = json.loads(rec.record_domain or "[]")
            except (TypeError, ValueError) as exc:
                raise ValidationError(_(
                    "Het record-domein is geen geldige JSON: %s",
                ) % exc) from exc
            if not isinstance(parsed, list):
                raise ValidationError(_(
                    "Het record-domein moet een lijst zijn."))


# De stand zoals hij in serveractie 1142 leefde, per agentnaam in plaats
# van per database-id, met twee correcties: ``website.page`` is eruit (dat
# model kan geen activiteit dragen) en Lux en Sem leveren op één vaste
# postbus in plaats van op willekeurig welk artikel. De record-ids zijn
# configuratie van déze database; een nieuwe klantdatabase krijgt zijn
# eigen regels via de blueprint-export.
SEED_ACTIVITY_SCOPES = {
    "Vera": [("knowledge.article", [["root_article_id", "=", 191]])],
    "Argus": [(
        "knowledge.article",
        [["id", "in", [182, 183, 184, 185, 186, 187]]],
    )],
    "Hilda": [(
        "helpdesk.ticket",
        [["close_date", "=", False], ["stage_id.fold", "=", False]],
    )],
    # Lux levert concepten af op de Conceptenbak, Sem zijn
    # WEB-WIJZIGING-voorstellen op de Zichtbaarheids-worklist.
    "Lux": [("knowledge.article", [["id", "=", 332]])],
    "Sem": [("knowledge.article", [["id", "=", 304]])],
    "Sanne": [
        ("crm.lead", []), ("sale.order", []), ("res.partner", []),
    ],
    "Pim": [("project.project", []), ("project.task", [])],
    "Daan": [("product.template", []), ("project.task", [])],
    "Bram": [
        ("crm.lead", []), ("sale.order", []), ("account.move", []),
        ("helpdesk.ticket", []), ("project.task", []),
    ],
    "Floris": [
        ("account.move", []), ("sale.order", []),
        ("project.project", []), ("project.task", []),
    ],
}


class AIAgent(models.Model):
    _inherit = "ai.agent"

    daadit_activity_scope_ids = fields.One2many(
        "daadit.ai.agent.activity.scope", "agent_id",
        string="Activity write scope",
    )

    @api.model
    def _daadit_seed_activity_scopes(self):
        """Zet de vastgelegde scoperegels neer waar ze nog missen.

        Idempotent en niet-verruimend: een agent die al regels heeft
        wordt niet aangeraakt, zodat een handmatige aanscherping niet
        stilletjes wordt teruggedraaid.
        """
        Scope = self.env["daadit.ai.agent.activity.scope"].sudo()
        created = 0
        for name, lines in SEED_ACTIVITY_SCOPES.items():
            agent = self.sudo().search([("name", "=", name)], limit=1)
            if not agent or agent.daadit_activity_scope_ids:
                continue
            for model_name, domain in lines:
                Scope.create({
                    "agent_id": agent.id,
                    "model_name": model_name,
                    "record_domain": json.dumps(domain),
                })
                created += 1
        _logger.info(
            "Activity scope seeding: %s regels aangemaakt", created)
        return created

    def _daadit_activity_capable(self, model_name):
        """Kan dit model een activiteit dragen?"""
        model = self.env.get(model_name)
        return model is not None and "activity_ids" in model._fields

    def _daadit_activity_scope(self, model_name, record_id):
        """Mag deze agent een activiteit plannen op dit record?

        Geeft ``(allowed, reason)`` terug. ``reason`` is leeg als het mag
        en anders de tekst die de agent te lezen krijgt — die noemt altijd
        de bestemmingen die wél werken.
        """
        self.ensure_one()
        scopes = self.daadit_activity_scope_ids.filtered("active")
        usable = [
            s.model_name for s in scopes
            if self._daadit_activity_capable(s.model_name)
        ]
        if not scopes:
            return False, _(
                "SCOPE-GUARD: voor deze agent is geen schrijfscope "
                "vastgelegd. Voeg een scoperegel toe voordat hij "
                "activiteiten mag plannen.")
        alternatives = ", ".join(usable) or _("geen enkel model")

        if not self._daadit_activity_capable(model_name):
            return False, _(
                "SCOPE-GUARD: %(model)s kan geen activiteit dragen "
                "(het model erft mail.activity.mixin niet). Plan je "
                "voorstel op %(alt)s en noem het record in de "
                "samenvatting.",
                model=model_name, alt=alternatives,
            )

        line = scopes.filtered(lambda s: s.model_name == model_name)
        if not line:
            return False, _(
                "SCOPE-GUARD: deze collega mag een activiteit "
                "uitsluitend plannen op %(alt)s — niet op %(model)s.",
                alt=alternatives, model=model_name,
            )

        target = self.env[model_name].sudo().browse(int(record_id or 0))
        if not target.exists():
            return False, _(
                "SCOPE-GUARD: %(model)s #%(rid)s bestaat niet — gebruik "
                "uitsluitend ids uit je laatste tool-resultaat.",
                model=model_name, rid=record_id,
            )

        domain = json.loads(line.record_domain or "[]")
        if domain and not target.filtered_domain(domain):
            return False, _(
                "SCOPE-GUARD: %(model)s #%(rid)s valt buiten de "
                "schrijfscope van deze collega — hard geblokkeerd "
                "(governance, tool-laag).",
                model=model_name, rid=record_id,
            )
        return True, ""
