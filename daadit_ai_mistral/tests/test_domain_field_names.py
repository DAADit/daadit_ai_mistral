# -*- coding: utf-8 -*-
"""Een verzonnen veld in het domein hoort de echte namen op te leveren.

Productie 14-8, run 832 (Marit, facturatie). Drie van haar vijf mislukte
tool-acties waren zoekopdrachten met een veld dat niet bestaat:

* actie 7553 — ``account.analytic.line.invoice_id``
* actie 7554 — ``project.task.type.is_closed`` (via ``stage_id.is_closed``)
* actie 7559 — ``account.analytic.account.is_internal``

De dispatcher gaf toen terug dat het domein "een JSON-lijst van triples"
moet zijn. Dat wás het al, dus de aanwijzing stuurde haar de verkeerde
kant op en drie beurten gingen op aan hetzelfde foute veld. Odoo kent de
echte veldnamen wel; deze tests leggen vast dat de tool ze noemt.
"""
from odoo.tests import common, tagged

from odoo.addons.daadit_ai_mistral.services import tool_dispatch as td


@tagged("post_install", "-at_install", "daadit_ai")
class TestOnbestaandVeldInDomein(common.TransactionCase):

    def test_verzonnen_veld_wordt_geweigerd_met_bestaande_namen(self):
        """De weigering noemt het model, het foute veld en echte namen."""
        domein = [["is_internal_klant", "=", False]]
        fout = td._domain_unknown_field_error(
            self.env, "account.analytic.line", domein,
            "ir_actions_server_search",
        )
        self.assertIsNotNone(fout)
        self.assertIn("is_internal_klant", fout["error"])
        self.assertIn("account.analytic.line", fout["error"])
        self.assertIn("get_fields", fout["error"])
        self.assertEqual(
            fout["unknown_domain_fields"], ["is_internal_klant"],
        )

    def test_bestaand_veld_gaat_gewoon_door(self):
        """Geen valse weigering: een geldig domein mag niets merken."""
        self.assertIsNone(td._domain_unknown_field_error(
            self.env, "project.task", [["name", "!=", False]],
            "ir_actions_server_search",
        ))

    def test_verzonnen_veld_achter_een_relatie_noemt_het_juiste_model(self):
        """Actie 7554: het foute veld zat op ``project.task.type``.

        De agent zocht op ``stage_id.is_closed`` en las in de fout de
        naam van het model waarop hij zocht. Het ontbrekende veld hoort
        bij het model achter de relatie; anders zoekt hij in de verkeerde
        veldenlijst verder.
        """
        fout = td._domain_unknown_field_error(
            self.env, "project.task", [["stage_id.is_closed", "=", True]],
            "ir_actions_server_search",
        )
        self.assertIsNotNone(fout)
        self.assertIn("project.task.type", fout["error"])
        self.assertIn("is_closed", fout["error"])

    def test_suggesties_bevatten_het_veld_dat_er_wel_is(self):
        """``partner_ids`` bestaat niet op de urenregel; ``partner_id`` wel."""
        namen = td._field_suggestions(
            self.env["account.analytic.line"], "partner_ids",
        )
        self.assertIn("partner_id", namen)
        self.assertTrue(namen, "verwacht minstens één bestaande veldnaam")
        for naam in namen:
            self.assertIn(
                naam, self.env["account.analytic.line"]._fields,
                "een suggestie moet een bestaand veld zijn",
            )

    def test_misbruik_domein_met_operatoren_en_rommel(self):
        """Misbruikgeval: een domein vol niet-blad-tokens mag niet omvallen.

        Een agent stuurt ``['&', ['x', '=', 1]]``, een los woord of een
        blad met een niet-string veldnaam. De controle mag daar geen
        uitzondering op gooien — een crash hier kost de hele run, niet
        alleen een beurt.
        """
        rommel = [
            "&", ["name", "!=", False], "|", 42, [1, "=", 1],
            ["verzonnen_veld_xyz", "=", 1],
        ]
        fout = td._domain_unknown_field_error(
            self.env, "project.task", rommel, "ir_actions_server_search",
        )
        self.assertIsNotNone(fout)
        self.assertEqual(
            fout["unknown_domain_fields"], ["verzonnen_veld_xyz"],
        )

    def test_onbekend_model_levert_geen_valse_weigering(self):
        """Een model dat wij niet kunnen beoordelen laten we passeren."""
        self.assertIsNone(td._domain_unknown_field_error(
            self.env, "model.dat.niet.bestaat", [["x", "=", 1]],
            "ir_actions_server_search",
        ))

    def test_orm_fout_krijgt_de_bestaande_namen_erbij(self):
        """Tweede net: ook buiten het domein hoort de hint erin te staan."""
        hint = td._invalid_field_hint(
            self.env,
            "Invalid field project.task.date_deadlinee in condition",
        )
        self.assertIn("date_deadline", hint)
        self.assertIn("project.task", hint)
        self.assertIn("get_fields", hint)

    def test_orm_fout_zonder_bruikbaar_model_blijft_leeg(self):
        """Zonder model geen hint: liever niets dan een verzonnen tip."""
        self.assertEqual(
            td._invalid_field_hint(self.env, "iets heel anders ging mis"),
            "",
        )


@tagged("post_install", "-at_install", "daadit_ai")
class TestZoekenZonderDomein(common.TransactionCase):
    """Actie 7563: zoeken zonder ``domain`` kostte een beurt.

    De dispatcher vult voor deze twee tools zelf ``domain="[]"`` in, maar
    dat gebeurde ná de controle op verplichte argumenten. Marit kreeg dus
    een weigering voor iets wat de tool al wist.
    """

    def test_zoektool_mag_zonder_domein_worden_aangeroepen(self):
        self.assertIn(
            "ir_actions_server_search", td._DEFAULT_EMPTY_DOMAIN_TOOLS,
        )
        self.assertIn(
            "ir_actions_server_read_group", td._DEFAULT_EMPTY_DOMAIN_TOOLS,
        )

    def test_misbruik_schrijftool_krijgt_geen_stil_standaarddomein(self):
        """Misbruikgeval: alleen zoektools mogen dit.

        Een schrijf- of verwijdertool zonder domein aanvullen met "alles"
        zou van een onvolledige aanroep een actie op de hele tabel maken.
        Die tools staan daarom niet in de lijst en moeten geweigerd
        blijven worden.
        """
        for naam in (
            "ir_actions_server_write",
            "ir_actions_server_unlink",
            "ir_actions_server_schedule_activity",
        ):
            self.assertNotIn(naam, td._DEFAULT_EMPTY_DOMAIN_TOOLS)
