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
from datetime import timedelta

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


# De code die in de serveractie achter de plan-activiteit-tool hoort te
# staan. Hij beslist niets meer zelf: de grens, de reparatiekanaal-
# uitzondering en de logging leven in dit bestand en zijn dus
# versiebeheerd, gereviewd en getest. De actie leest alleen de optionele
# parameters uit — die bestaan als naam niet in de eval-context zodra het
# model ze niet meestuurt, en Odoo's safe-eval laat geen getattr-truc toe.
SERVER_ACTION_CODE = '''# SCOPE-GUARD — de beslissing staat in daadit_ai_mistral,
# model ai.agent, methode _daadit_schedule_activity_guarded.
# Wijzig de grens daar en niet hier: deze code is een doorgeefluik.
try:
    _ati = activity_type_id
except Exception:
    _ati = False
try:
    _atx = activity_type_xmlid
except Exception:
    _atx = False
try:
    _note = note
except Exception:
    _note = ''
try:
    _deadline = date_deadline
except Exception:
    _deadline = False
try:
    _uid = user_id
except Exception:
    _uid = False
try:
    _summary = summary
except Exception:
    _summary = ''

ai['result'] = record._daadit_schedule_activity_guarded(
    model_name=model_name,
    record_id=record_id,
    activity_type_id=_ati,
    activity_type_xmlid=_atx,
    summary=_summary,
    note=_note,
    date_deadline=_deadline,
    user_id=_uid,
)
'''

# Een AUTO-APPLY van de assurance-watchdog is geen herinnering maar een
# opdracht aan de applier. De cap op open to-do's per record hoort er niet
# op te gelden — zonder deze uitzondering liep artikel 182 vol en werd
# twaalf dagen lang elke autonome reparatie geweigerd (taak 722).
REPAIR_CHANNEL_PREFIX = "AUTO-APPLY"
REPAIR_CHANNEL_BACKLOG = 5


class AIAgent(models.Model):
    _inherit = "ai.agent"

    daadit_activity_scope_ids = fields.One2many(
        "daadit.ai.agent.activity.scope", "agent_id",
        string="Activity write scope",
    )
    daadit_repair_channel = fields.Boolean(
        string="Repair channel (AUTO-APPLY)", default=False,
        help="Deze agent mag reparatievoorstellen indienen langs de cap op "
             "open herinneringen. Alleen voor de assurance-watchdog; de "
             "schrijfscope blijft onverkort gelden.",
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

    def _daadit_schedule_activity_guarded(
        self, model_name, record_id, summary=None, note=None,
        date_deadline=False, user_id=False, activity_type_id=False,
        activity_type_xmlid=False,
    ):
        """Plan een activiteit, of weiger met uitleg.

        Dit is het enige pad waarlangs een agent een activiteit plant.
        De serveractie achter de tool roept alleen deze methode aan
        (:data:`SERVER_ACTION_CODE`), zodat de grens niet meer in een
        databaseveld leeft.
        """
        self.ensure_one()
        allowed, reason = self._daadit_activity_scope(model_name, record_id)
        if allowed and not (summary or "").strip():
            allowed = False
            reason = _(
                "SCOPE-GUARD: een activiteit zonder samenvatting is "
                "nutteloos voor de ontvanger. Roep de tool opnieuw aan "
                "met een korte, concrete summary.")
        if not allowed:
            _logger.warning(
                "SCOPE-GUARD blokkeerde write: agent %s -> %s #%s",
                self.id, model_name, record_id)
            return {
                "ok": False,
                "blocked_by_scope_guard": True,
                "error": reason,
            }

        if not activity_type_id and not activity_type_xmlid:
            activity_type_xmlid = "mail.mail_activity_data_todo"

        is_repair = (
            self.daadit_repair_channel
            and (summary or "").strip().upper().startswith(
                REPAIR_CHANNEL_PREFIX)
        )
        if is_repair:
            return self._daadit_repair_channel_activity(
                model_name, int(record_id), summary, note,
                date_deadline, user_id, activity_type_id)

        return self._ai_tool_schedule_activity(
            model_name=model_name,
            record_id=record_id,
            activity_type_xmlid=activity_type_xmlid,
            activity_type_id=activity_type_id,
            summary=summary,
            note=note,
            date_deadline=date_deadline,
            user_id=user_id,
        )

    def _daadit_repair_channel_activity(
        self, model_name, record_id, summary, note, date_deadline,
        user_id, activity_type_id,
    ):
        """Dien een reparatievoorstel in, langs de herinneringencap.

        Met eigen dedup (exact dezelfde samenvatting bestaat al) en een
        bovengrens: staan er vijf onverwerkte voorstellen van het
        afgelopen etmaal, dan hapert de applier zelf en helpt een zesde
        voorstel niemand.
        """
        self.ensure_one()
        Act = self.env["mail.activity"].sudo()
        base = [("res_model", "=", model_name), ("res_id", "=", record_id)]
        same = Act.search(base + [("summary", "=", summary)], limit=1)
        if same:
            return {
                "ok": True, "skipped": True,
                "reason": "duplicate_autoapply",
                "activity_id": same.id,
                "message": _(
                    "Er staat al een openstaand AUTO-APPLY-voorstel met "
                    "exact deze samenvatting (activiteit %s). Er is niets "
                    "aangemaakt; rapporteer dat en dien geen tweede "
                    "voorstel voor hetzelfde blok in.", same.id),
            }

        since = fields.Datetime.to_string(
            fields.Datetime.now() - timedelta(days=1))
        pending = Act.search_count(base + [
            ("summary", "ilike", REPAIR_CHANNEL_PREFIX),
            ("create_date", ">=", since),
        ])
        if pending >= REPAIR_CHANNEL_BACKLOG:
            return {
                "ok": False, "error": "applier_backlog",
                "message": _(
                    "Er staan al %s onverwerkte AUTO-APPLY-voorstellen van "
                    "het afgelopen etmaal op dit record. De applier "
                    "verwerkt ze normaal binnen tien minuten; dat gebeurt "
                    "kennelijk niet. Er is niets aangemaakt — meld dit als "
                    "storing in de herstelketen zelf.", pending),
            }

        owner = int(user_id or 0) or self._daadit_repair_channel_user()
        new = Act.create({
            "res_model_id": self.env["ir.model"]._get_id(model_name),
            "res_id": record_id,
            "activity_type_id": (
                int(activity_type_id) if activity_type_id
                else self.env.ref("mail.mail_activity_data_todo").id),
            "user_id": owner,
            "summary": summary,
            "note": note or "",
            "date_deadline": date_deadline or fields.Date.context_today(self),
        })
        _logger.warning(
            "REPARATIEKANAAL: AUTO-APPLY %s aangemaakt op %s #%s langs de "
            "herinneringencap.", new.id, model_name, record_id)
        return {
            "ok": True, "activity_id": new.id, "model_name": model_name,
            "record_id": record_id, "summary": summary,
            "user_id": owner, "bypassed_reminder_cap": True,
            "message": _(
                "Reparatievoorstel ingediend. De applier pikt het binnen "
                "tien minuten op."),
        }

    def _daadit_repair_channel_user(self):
        """Wie krijgt een reparatievoorstel als niemand is meegegeven?"""
        param = self.env["ir.config_parameter"].sudo().get_param(
            "daadit_ai_mistral.repair_channel_user_id")
        if param and param.isdigit() and int(param):
            return int(param)
        return self.env.ref("base.user_admin").id
