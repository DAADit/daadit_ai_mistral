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
    # De financiële bezetting onder Floris. Elk van de vier levert op één
    # eigen artikel af; de bredere modellen staan erbij zodat een
    # bevinding ook op het record zelf zichtbaar wordt. Geen van hen mag
    # een boeking, factuur of prijs wijzigen — dat blijft buiten de
    # activiteit-scope en buiten hun modellijst.
    "Bo": [
        ("knowledge.article", [["id", "=", 350]]),
        ("account.move", []),
    ],
    "Dirk": [
        ("knowledge.article", [["id", "=", 351]]),
        ("account.move", [["move_type", "=", "out_invoice"]]),
    ],
    "Marit": [
        ("knowledge.article", [["id", "=", 352]]),
        ("sale.order", []),
    ],
    "Coen": [
        ("knowledge.article", [["id", "=", 353]]),
        ("res.partner", []),
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

# Woorden die niets over het onderwerp zeggen. Ze staan apart van de
# stopwoorden in ``ai_agent.py``: die dienen de vergelijking van twee
# samenvattingen, deze de vraag of een samenvatting überhaupt iets
# meldt. "Openstaand actiepunt afhandelen" bestaat volledig uit deze
# woorden en is daarmee net zo leeg als "Opvolgen" — terwijl het met 31
# tekens elke lengte-eis haalt. De laatste regel zijn kanaallabels: ze
# zeggen langs welke weg het bericht komt, niet waar het over gaat.
HOLLOW_SUMMARY_WORDS = frozenset("""
    actie acties actiepunt actiepunten punt punten item items
    taak taken todo to do ding dingen werk iets
    opvolgen opvolging opvolgactie afhandelen afhandeling oppakken
    doen uitvoeren behandelen verwerken checken nakijken bijwerken
    openstaand openstaande open resterend resterende
    vereist verplicht nodig noodzakelijk gewenst
    urgent dringend spoed spoedig belangrijk aandacht attentie
    graag svp aub alsjeblieft please asap direct meteen nu vandaag
    follow followup up pending required needed action attention
    voorstel toepasbaar geweigerd weigering vangrail
""".split())

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

    def _daadit_domain_mismatch_reason(self, target, domain):
        """Welke voorwaarden uit het scoperecord weigeren dit record?

        Zonder dit is de weigering blind: "valt buiten de schrijfscope"
        vertelt een agent niet dat het ticket gesloten is, dus probeert
        hij het volgende gesloten ticket net zo goed. Met de voorwaarde
        erbij kan hij zelf een geldig record kiezen. De tekst komt uit
        het domein van het scoperecord en niet uit een lijst
        fasenamen in de prompt — de beslissing blijft dus in de records.
        """
        failed = []
        for leaf in domain:
            if not isinstance(leaf, (list, tuple)) or len(leaf) != 3:
                # '&' / '|' / '!' zijn geen voorwaarde om te tonen.
                continue
            if not target.filtered_domain([list(leaf)]):
                failed.append(
                    "%s %s %s" % (leaf[0], leaf[1], leaf[2]))
        return failed

    def _daadit_activity_scope(self, model_name, record_id):
        """Mag deze agent een activiteit plannen op dit record?"""
        return self._daadit_scope_check(
            model_name, record_id, need_activity=True)

    def _daadit_write_scope(self, model_name, record_id):
        """Mag deze agent dit record wijzigen? (taak 1079)

        ``AI: Assign User`` schreef ``user_id`` op elk record dat binnen
        de *lees*scope viel: een gesloten ticket, een gevouwen fase, een
        model dat deze collega alleen mag lézen. De schrijfgrens per
        collega stond al in records, maar alleen het plannen van een
        activiteit vroeg hem op. Dezelfde scoperecords gelden nu voor
        beide schrijftools; het toewijzen vraagt geen
        ``mail.activity.mixin``, dus die eis valt hier weg.
        """
        return self._daadit_scope_check(
            model_name, record_id, need_activity=False)

    def _daadit_scope_check(self, model_name, record_id, need_activity=True):
        """De schrijfgrens zelf.

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
        if not need_activity:
            usable = [s.model_name for s in scopes]
        alternatives = ", ".join(usable) or _("geen enkel model")

        if need_activity and not self._daadit_activity_capable(model_name):
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
                "SCOPE-GUARD: deze collega mag uitsluitend schrijven op "
                "%(alt)s — niet op %(model)s.",
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
            failed = self._daadit_domain_mismatch_reason(target, domain)
            return False, _(
                "SCOPE-GUARD: %(model)s #%(rid)s valt buiten de "
                "schrijfscope van deze collega — hard geblokkeerd "
                "(governance, tool-laag). Niet aan voldaan: %(failed)s. "
                "Wat wél mag: %(where)s. Kies een record uit je laatste "
                "zoekresultaat dat daaraan voldoet; dezelfde poging "
                "opnieuw wordt weer geweigerd.",
                model=model_name, rid=record_id,
                failed="; ".join(failed) or json.dumps(domain),
                where=self._daadit_scope_hint(line),
            )
        return True, ""

    @api.model
    def _daadit_scope_hint(self, line):
        """Waar deze scoperegel wél naartoe leidt, in leesbare vorm.

        Een weigering die alleen "buiten de scope" zegt laat het model
        gokken, en gokken kost tool-acties zonder resultaat — precies
        wat de website.page-weigering hierboven al opleverde. Daarom
        noemt de tekst het model plus de voorwaarde uit het
        record-domein: bij de Vault Agent is dat
        ``root_article_id = 191``, dus de hele Vault-boom.
        """
        try:
            domain = json.loads(line.record_domain or "[]")
        except (TypeError, ValueError):
            domain = []
        parts = []
        for condition in domain:
            if isinstance(condition, (list, tuple)) and len(condition) == 3:
                parts.append("%s %s %s" % tuple(condition))
        if not parts:
            return _("elk record van %s") % line.model_name
        return _("%(model)s waarvoor geldt: %(cond)s") % {
            "model": line.model_name,
            "cond": " en ".join(parts),
        }

    @api.model
    def _daadit_summary_is_hollow(self, summary):
        """Meldt deze samenvatting iets, of alleen dát er iets is?

        Waarom geen tekenlengte. Het voorstel van de zelfherstelloop
        (11-08-2026) wilde minimaal 12 tekens eisen. Dat criterium meet
        het verkeerde: wat de Vault Agent in productie wegschreef was
        "Openstaand actiepunt afhandelen" — 31 tekens, drie keer op een
        klantartikel, en nog steeds zonder onderwerp. Omgekeerd zegt
        "Actie vereist nu" met zestien tekens niets terwijl
        "SLA-bijlage mist" met evenveel tekens een melding is.

        Daarom kijkt deze controle naar inhoud: blijft er na het
        wegstrepen van het reparatiekanaal-voorvoegsel, de stopwoorden
        en de holle woorden nog een woord over dat het onderwerp
        benoemt? Een los getal telt niet mee — "182" is geen melding.
        """
        text = (summary or "").strip()
        if not text:
            return True
        # "⚠️ AUTO-APPLY niet toepasbaar: opvolgen" mag niet door de
        # controle glippen op zijn voorvoegsel: het kanaal zegt waar het
        # bericht vandaan komt, niet wat er aan de hand is.
        marker = text.upper().find(REPAIR_CHANNEL_PREFIX)
        if marker != -1:
            text = text[marker + len(REPAIR_CHANNEL_PREFIX):]
        meaningful = [
            token for token in self._daadit_activity_tokens(text)
            if token not in HOLLOW_SUMMARY_WORDS and not token.isdigit()
        ]
        return not meaningful

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
        reason_code = "scope"
        if allowed and not (summary or "").strip():
            allowed = False
            reason_code = "summary_leeg"
            reason = _(
                "SCOPE-GUARD: een activiteit zonder samenvatting is "
                "nutteloos voor de ontvanger. Roep de tool opnieuw aan "
                "met een korte, concrete summary.")
        elif allowed and self._daadit_summary_is_hollow(summary):
            allowed = False
            reason_code = "summary_zonder_inhoud"
            reason = _(
                "SCOPE-GUARD: de samenvatting %(summary)s benoemt geen "
                "onderwerp — wie hem in zijn lijst ziet weet nog niets. "
                "Noem waar het over gaat (het document, het veld, de "
                "klant of de constatering), niet dat er iets moet "
                "gebeuren. Roep de tool opnieuw aan; de lengte is niet "
                "het probleem.",
                summary=(summary or "").strip(),
            )
        if not allowed:
            _logger.warning(
                "SCOPE-GUARD blokkeerde write: agent %s -> %s #%s",
                self.id, model_name, record_id)
            return {
                "ok": False,
                "blocked_by_scope_guard": True,
                "reason": reason_code,
                "error": reason,
            }

        if not activity_type_id and not activity_type_xmlid:
            activity_type_xmlid = "mail.mail_activity_data_todo"

        is_repair = (
            self.daadit_repair_channel
            and self._daadit_summary_starts_with_token(
                summary, REPAIR_CHANNEL_PREFIX,
            )
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
                # Honest envelope (taak 779): skip ≠ created.
                "ok": False,
                "written": False,
                "skipped": True,
                "reason": "duplicate_autoapply",
                "existing_activity_id": same.id,
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
