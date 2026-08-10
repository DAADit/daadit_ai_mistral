# -*- coding: utf-8 -*-
"""Tests for the argument-name repair and the result cap (taken 741,
742, 744).

All three came out of the action log, not out of a review: agents kept
losing iterations on refusals that named the wrong problem, and an
over-cap result was thrown away entirely so the model asked again with
the same query.
"""
import json

from odoo.tests import common, tagged

from odoo.addons.daadit_ai_mistral.services import tool_dispatch as td


class _FakeAction:
    """Stand-in for ``ir.actions.server`` — the helpers only read
    ``ai_tool_schema``."""

    def __init__(self, properties, required=()):
        self.ai_tool_schema = json.dumps({
            "type": "object",
            "properties": {p: {"type": "string"} for p in properties},
            "required": list(required),
        })


@tagged("post_install", "-at_install", "daadit_ai")
class TestArgNameRepair(common.TransactionCase):

    def test_dutch_keys_land_on_the_english_schema(self):
        """The exact call from run 556 that was refused four times in a
        row while carrying every value it needed."""
        action = _FakeAction(
            ["campaign_id", "title", "channel", "date", "assignee"],
            required=["campaign_id", "title", "channel", "date"],
        )
        kwargs = {
            "campagne_id": 7,
            "onderwerp": "Zomercampagne week 32",
            "kanaal": "linkedin",
            "datum": "2026-08-10",
            "uitvoerder": "Mark",
        }
        out, renames = td._remap_arg_names(action, kwargs)
        self.assertEqual(td._missing_required_args(action, out), [])
        self.assertEqual(out["title"], "Zomercampagne week 32")
        self.assertEqual(out["campaign_id"], 7)
        self.assertEqual(renames["kanaal"], "channel")

    def test_case_and_punctuation_differences(self):
        action = _FakeAction(["campaign_id", "title"], ["campaign_id"])
        out, _ = td._remap_arg_names(action, {"Campaign ID": 3})
        self.assertEqual(out["campaign_id"], 3)

    def test_bare_name_of_an_id_parameter(self):
        action = _FakeAction(["partner_id"], ["partner_id"])
        out, _ = td._remap_arg_names(action, {"partner": 42})
        self.assertEqual(out["partner_id"], 42)

    def test_never_overwrites_what_the_model_meant(self):
        """If both the schema name and an alias arrive, the schema name
        wins — moving the alias on top of it would silently replace a
        deliberate value."""
        action = _FakeAction(["title"], ["title"])
        out, renames = td._remap_arg_names(
            action, {"title": "Engels", "onderwerp": "Nederlands"},
        )
        self.assertEqual(out["title"], "Engels")
        self.assertEqual(renames, {})

    def test_unknown_key_is_not_guessed_onto_a_missing_one(self):
        """One unknown key plus one missing parameter is not evidence
        they belong together."""
        action = _FakeAction(["channel", "title"], ["channel"])
        out, renames = td._remap_arg_names(action, {"kleur": "blauw"})
        self.assertEqual(renames, {})
        self.assertEqual(td._missing_required_args(action, out), ["channel"])

    def test_schemaless_action_is_left_alone(self):
        action = _FakeAction([])
        action.ai_tool_schema = ""
        payload = {"whatever": 1}
        out, renames = td._remap_arg_names(action, payload)
        self.assertIs(out, payload)
        self.assertEqual(renames, {})


@tagged("post_install", "-at_install", "daadit_ai")
class TestResultCap(common.TransactionCase):

    def _set_cap(self, value):
        self.env["ir.config_parameter"].sudo().set_param(
            td._RESULT_CAP_ICP, value,
        )

    def test_configured_cap_wins_over_the_constant(self):
        """Production had the parameter on 6000 while the code enforced
        50 000, so the number in the error message was not the number
        being applied."""
        self._set_cap("6000")
        self.assertEqual(td._result_cap_chars(self.env), 6000)

    def test_invalid_cap_falls_back(self):
        self._set_cap("nonsense")
        self.assertEqual(
            td._result_cap_chars(self.env), td._MAX_TOOL_RESULT_CHARS,
        )

    def test_zero_disables_the_cap(self):
        self._set_cap("0")
        big = [{"id": i, "name": "x" * 200} for i in range(500)]
        self.assertIs(
            td._enforce_result_size_cap(big, "search", env=self.env), big,
        )

    def test_oversized_list_is_truncated_not_refused(self):
        self._set_cap("6000")
        big = [{"id": i, "name": "x" * 200} for i in range(500)]
        out = td._enforce_result_size_cap(big, "search", env=self.env)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["total_records"], 500)
        self.assertGreater(out["returned_records"], 0)
        self.assertLess(out["returned_records"], 500)
        self.assertNotIn(
            "error", out,
            "A truncated result is usable data, not a failed call",
        )
        self.assertLessEqual(
            len(json.dumps(out["records"], default=str)), 6000,
        )
        self.assertEqual(
            out["records"][0], big[0], "Kept records must be verbatim",
        )

    def test_oversized_non_list_keeps_a_usable_head(self):
        self._set_cap("6000")
        out = td._enforce_result_size_cap(
            {"report": "y" * 40000}, "read", env=self.env,
        )
        self.assertTrue(out["truncated"])
        self.assertTrue(out["partial_result"])
        self.assertLessEqual(len(out["partial_result"]), 6000)

    def test_result_within_cap_is_untouched(self):
        self._set_cap("6000")
        small = [{"id": 1, "name": "ok"}]
        self.assertIs(
            td._enforce_result_size_cap(small, "search", env=self.env),
            small,
        )


@tagged("post_install", "-at_install", "daadit_ai")
class TestDomainGroupFlattening(common.TransactionCase):
    """Run 565 (Eva) lost 14 of its 46 tool calls to one domain shape:
    an OR wrapped in its own list, which the ORM reads as a leaf and
    dies on with ``'list' object has no attribute 'lower'``."""

    def test_wrapped_or_group_is_spliced_into_the_parent(self):
        domain = [
            ["active", "=", True],
            ["|", ["date", ">=", "2026-07-01"], ["date", "=", False]],
        ]
        self.assertEqual(
            td._coerce_domain_obj(domain),
            [
                ["active", "=", True],
                "|",
                ["date", ">=", "2026-07-01"],
                ["date", "=", False],
            ],
        )

    def test_nested_groups_flatten_completely(self):
        domain = [["|", ["|", ["a", "=", 1], ["b", "=", 2]],
                   ["c", "=", 3]]]
        self.assertEqual(
            td._coerce_domain_obj(domain),
            ["|", "|", ["a", "=", 1], ["b", "=", 2], ["c", "=", 3]],
        )

    def test_negation_group_is_flattened_too(self):
        self.assertEqual(
            td._coerce_domain_obj([["!", ["a", "=", 1]]]),
            ["!", ["a", "=", 1]],
        )

    def test_correct_prefix_domain_is_left_alone(self):
        domain = ["|", ["a", "=", 1], ["b", "=", 2]]
        self.assertEqual(td._coerce_domain_obj(domain), domain)

    def test_leaf_with_a_list_value_is_not_a_group(self):
        domain = [["id", "in", [1, 2, 3]]]
        self.assertEqual(td._coerce_domain_obj(domain), domain)

    def test_operator_as_a_leaf_value_is_not_a_group(self):
        domain = [["name", "like", "|"]]
        self.assertEqual(td._coerce_domain_obj(domain), domain)
