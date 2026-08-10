# -*- coding: utf-8 -*-
"""Tests voor de zeef op het uitgaande antwoord (mail.message 27078).

Robin schreef op 3-8-2026 zijn eigen antwoord, plakte daaronder de
deelvraag die hij aan Pim had gesteld plus het ruwe JSON-resultaat, en
liep daarna vast in een lus van scheidingstekens. Alle drie stonden in
de chat.
"""
from odoo.tests import common, tagged

from odoo.addons.daadit_ai_mistral.services.llm_api_patch import (
    _EMPTY_AFTER_STRIP,
    _clean_adapted,
    _strip_runaway_and_leaks,
)


@tagged("post_install", "-at_install", "daadit_ai")
class TestAnswerSanitizer(common.TransactionCase):

    def test_normal_answer_is_untouched(self):
        text = (
            "Er zijn vandaag geen projecttaken afgerond.\n\n"
            "- Wel drie taken in behandeling\n"
            "- Eén taak wacht op jou\n\n— Robin"
        )
        cleaned, trimmed = _strip_runaway_and_leaks(text)
        self.assertFalse(trimmed)
        self.assertEqual(cleaned, text)

    def test_markdown_separator_survives(self):
        text = "Kop\n\n---\n\nTekst onder de streep."
        cleaned, trimmed = _strip_runaway_and_leaks(text)
        self.assertFalse(trimmed)
        self.assertEqual(cleaned, text)

    def test_leaked_delegation_and_json_are_cut(self):
        """Het live bericht, ingekort."""
        text = (
            "Ik heb dit zelf opgehaald uit het systeem.\n\n— Robin\n"
            "]]   -   Pim Wat is de status van alle projecttaken die "
            "vandaag zijn afgerond in Odoo?   "
            '{ "answer": "Vandaag zijn er geen taken afgerond.", '
            '"error": null }'
        )
        cleaned, trimmed = _strip_runaway_and_leaks(text)
        self.assertTrue(trimmed)
        self.assertEqual(
            cleaned,
            "Ik heb dit zelf opgehaald uit het systeem.\n\n— Robin",
        )

    def test_bare_tool_result_json_is_cut(self):
        text = 'Klaar.\n{"ok": true, "answer": "x", "error": null}'
        cleaned, trimmed = _strip_runaway_and_leaks(text)
        self.assertTrue(trimmed)
        self.assertEqual(cleaned, "Klaar.")

    def test_json_without_error_field_is_kept(self):
        """Een antwoord dat zélf over JSON gaat is geen lek."""
        text = 'Stuur het als {"answer": "ja"} naar de endpoint.'
        cleaned, trimmed = _strip_runaway_and_leaks(text)
        self.assertFalse(trimmed)
        self.assertEqual(cleaned, text)

    def test_runaway_tail_is_cut(self):
        text = "Overzicht staat hieronder.\n\n" + ">" * 400
        cleaned, trimmed = _strip_runaway_and_leaks(text)
        self.assertTrue(trimmed)
        self.assertEqual(cleaned, "Overzicht staat hieronder.")

    def test_non_string_and_empty_input_do_not_crash(self):
        for value in (None, "", 42, {"a": 1}):
            cleaned, trimmed = _strip_runaway_and_leaks(value)
            self.assertFalse(trimmed)
            self.assertEqual(cleaned, value)

    def test_clean_adapted_handles_strings_and_dicts(self):
        leak = 'Klaar.\n{"answer": "x", "error": null}'
        out = _clean_adapted(
            [leak, {"role": "assistant", "content": leak}, 7],
            "test",
        )
        self.assertEqual(out[0], "Klaar.")
        self.assertEqual(
            out[1], {"role": "assistant", "content": "Klaar."},
        )
        self.assertEqual(out[2], 7)

    def test_nothing_left_yields_a_notice_not_an_empty_message(self):
        out = _clean_adapted(['{"answer": "x", "error": null}'], "test")
        self.assertEqual(out, [_EMPTY_AFTER_STRIP])
