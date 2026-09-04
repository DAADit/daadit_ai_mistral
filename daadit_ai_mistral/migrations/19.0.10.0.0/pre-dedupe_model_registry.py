# -*- coding: utf-8 -*-
"""Ruim dubbele ``technical_name`` op voordat de unieke index erop komt.

19.0.6.29.5 verving de ``_sql_constraints``-lijst door ``models.Constraint``.
Odoo 19 negeerde die lijst stilzwijgend, dus de unieke index heeft nooit
bestaan en de sync kon dezelfde model-id twee keer wegschrijven: een
gearchiveerde rij werd niet gevonden en dus opnieuw aangemaakt. Op
productie stonden 26 van die paren en meldde de upgrade:

    odoo.schema: could not create unique index
        "daadit_ai_mistral_model_technical_name_uniq"

De sync zoekt inmiddels met ``active_test=False`` en maakt geen nieuwe
duplicaten meer; wat rest is de historische rommel. Zonder deze opruiming
mislukt het aanleggen van de index bij elke update opnieuw, en bestaat de
garantie dus alsnog niet.
"""
TABLE = "daadit_ai_mistral_model"
MODEL = "daadit.ai.mistral.model"


def migrate(cr, version):
    if not version:
        return

    cr.execute("SELECT to_regclass(%s) IS NOT NULL", ("public." + TABLE,))
    if not cr.fetchone()[0]:
        # Verse database: de ORM legt de tabel zo dadelijk zelf aan, leeg
        # en dus per definitie zonder duplicaten.
        return

    # Winnaar per technical_name: de actieve rij (die staat in de
    # llm_model-keuzelijst), bij gelijke stand de oudste. Bewust NIET de
    # rij met het xml-id: op productie had juist de gearchiveerde helft
    # het xml-id, en die laten winnen zou het levende model archiveren.
    cr.execute(
        """
        SELECT id,
               first_value(id) OVER w AS keeper,
               row_number()    OVER w AS rn
          FROM {table}
         WHERE technical_name IS NOT NULL
        WINDOW w AS (
            PARTITION BY technical_name
            ORDER BY COALESCE(active, FALSE) DESC, id ASC
        )
        """.format(table=TABLE)
    )
    moves = [(row[0], row[1]) for row in cr.fetchall() if row[2] > 1]
    if not moves:
        return

    # Het xml-id verhuist mee in plaats van te verdwijnen. Een xml-id dat
    # naar een verwijderde rij wijst laat de seed-data hem bij de volgende
    # update opnieuw aanmaken -- en dan staat het duplicaat er weer.
    for loser, keeper in moves:
        cr.execute(
            "UPDATE ir_model_data SET res_id = %s "
            " WHERE model = %s AND res_id = %s",
            (keeper, MODEL, loser),
        )
    cr.execute(
        "DELETE FROM {table} WHERE id = ANY(%s)".format(table=TABLE),
        ([loser for loser, _ in moves],),
    )
