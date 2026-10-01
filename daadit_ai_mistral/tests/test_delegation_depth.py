# -*- coding: utf-8 -*-
"""Delegation: two levels deep, no loops, routing on role, sub-run bails."""
import sys
import types
from unittest.mock import patch

from odoo.tests import common, tagged

from odoo.addons.daadit_ai_mistral.services import llm_api_patch
from odoo.addons.daadit_ai_mistral.services import tool_dispatch as td


class _FakeService:
    """Stands in for ``LLMApiService``: each agent runs a scripted turn."""

    calls = []
    script = {}

    def __init__(self, env=None, provider=None):
        self.provider = provider

    def request_llm(self, model=None, inputs=None, tools=None):
        agent = td.current_agent.record
        _FakeService.calls.append({
            "agent": agent,
            "depth": td.router_state.depth,
            "tools": list(tools or []),
            "provider": self.provider,
        })
        turn = _FakeService.script.get(agent.id)
        if turn:
            return turn()
        return "Antwoord van %s" % agent.name


class _Recorder:
    def __init__(self):
        self.events = []

    def begin(self, caller, target, question, depth):
        self.events.append(("begin", caller.name, target.name, depth))
        return len(self.events)

    def end(self, token, result):
        self.events.append(("end", token, bool(result and result.get("ok"))))


@tagged("post_install", "-at_install", "daadit_ai")
class TestDelegationDepth(common.TransactionCase):

    def setUp(self):
        super().setUp()
        Agent = self.env["ai.agent"]
        self.robin = Agent.create({
            "name": "Test-Robin", "daadit_is_orchestrator": True,
        })
        self.bram = Agent.create({"name": "Test-Bram"})
        self.pim = Agent.create({"name": "Test-Pim"})
        self.daan = Agent.create({"name": "Test-Daan"})
        for agent in (self.robin, self.bram, self.pim, self.daan):
            agent.llm_model = "mistral-large-latest"
        _FakeService.calls = []
        _FakeService.script = {}
        fake_mod = types.ModuleType("odoo.addons.ai.utils.llm_api_service")
        fake_mod.LLMApiService = _FakeService
        fake_pkg = types.ModuleType("odoo.addons.ai.utils")
        fake_pkg.llm_api_service = fake_mod
        for p in (
            patch.dict(sys.modules, {
                "odoo.addons.ai.utils": fake_pkg,
                "odoo.addons.ai.utils.llm_api_service": fake_mod,
            }),
            patch.object(llm_api_patch, "patch_llm_api_service", lambda: True),
            patch.object(llm_api_patch, "_notify_step", lambda *a, **k: None),
            patch.object(td, "delegation_hooks", None),
        ):
            p.start()
            self.addCleanup(p.stop)
        td.begin_top_level_turn()
        td.router_state.depth = 0
        td.current_agent.record = self.robin
        self.addCleanup(self._reset_state)

    def _reset_state(self):
        td.begin_top_level_turn()
        td.router_state.depth = 0
        td.current_agent.record = None

    def _ask(self, caller, name):
        return caller._ai_tool_ask_agent(agent_name=name, question="Hoe staat het?")

    def test_two_levels_are_allowed(self):
        _FakeService.script[self.bram.id] = lambda: self._ask(
            self.bram, "Test-Pim",
        )["answer"]
        result = self._ask(self.robin, "Test-Bram")
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["answer"], "Antwoord van Test-Pim")
        self.assertEqual(
            [(c["agent"], c["depth"]) for c in _FakeService.calls],
            [(self.bram, 1), (self.pim, 2)],
        )

    def test_third_level_is_refused(self):
        seen = {}

        def pim_turn():
            seen["result"] = self._ask(self.pim, "Test-Daan")
            return "Zelf uitgezocht"

        _FakeService.script[self.bram.id] = lambda: self._ask(
            self.bram, "Test-Pim",
        )["answer"]
        _FakeService.script[self.pim.id] = pim_turn
        result = self._ask(self.robin, "Test-Bram")
        self.assertTrue(result.get("ok"), result)
        self.assertIn("depth limit", seen["result"]["error"])
        self.assertNotIn(self.daan, [c["agent"] for c in _FakeService.calls])

    def test_routing_back_up_the_chain_is_refused(self):
        seen = {}

        def bram_turn():
            seen["result"] = self._ask(self.bram, "Test-Robin")
            return "Zelf uitgezocht"

        _FakeService.script[self.bram.id] = bram_turn
        self._ask(self.robin, "Test-Bram")
        self.assertIn("loop", seen["result"]["error"])

    def test_router_tool_only_offered_while_another_level_fits(self):
        self.assertTrue(td.subrun_tool_allowed(td.ROUTER_TOOL_SLUG, 1))
        self.assertFalse(td.subrun_tool_allowed(td.ROUTER_TOOL_SLUG, 2))
        self.assertFalse(td.subrun_tool_allowed(td.OPEN_CHAT_TOOL_SLUG, 1))
        self.assertFalse(td.subrun_tool_allowed(
            "ir_actions_server_assign_user", 1,
        ))
        self.assertTrue(td.subrun_tool_allowed("ir_actions_server_search", 2))

    def test_subrun_bail_fails_the_hop_not_the_parent_run(self):
        def bram_turn():
            td.flag_bail("max_iter")
            return "Ik ben halverwege"

        _FakeService.script[self.bram.id] = bram_turn
        result = self._ask(self.robin, "Test-Bram")
        self.assertIn("could not complete", result["error"])
        self.assertFalse(td.router_state.top_level_exhausted)
        self.assertFalse(td.router_state.exhausted)

    def test_top_level_bail_marks_the_turn(self):
        td.flag_bail("deadline")
        self.assertTrue(td.router_state.top_level_exhausted)
        self.assertEqual(td.router_state.exhaustion_reason, "deadline")

    def test_state_is_restored_after_nested_routes(self):
        _FakeService.script[self.bram.id] = lambda: self._ask(
            self.bram, "Test-Pim",
        )["answer"]
        self._ask(self.robin, "Test-Bram")
        self.assertEqual(td.router_state.depth, 0)
        self.assertEqual(td.router_state.chain, ())
        self.assertEqual(td.current_agent.record, self.robin)
        self.assertEqual(td.router_state.calls, 2)

    def test_hooks_see_every_level(self):
        recorder = _Recorder()
        td.delegation_hooks = recorder
        _FakeService.script[self.bram.id] = lambda: self._ask(
            self.bram, "Test-Pim",
        )["answer"]
        self._ask(self.robin, "Test-Bram")
        self.assertEqual(recorder.events, [
            ("begin", "Test-Robin", "Test-Bram", 1),
            ("begin", "Test-Bram", "Test-Pim", 2),
            ("end", 2, True),
            ("end", 1, True),
        ])


@tagged("post_install", "-at_install", "daadit_ai")
class TestRoutingOnRole(common.TransactionCase):

    def setUp(self):
        super().setUp()
        Agent = self.env["ai.agent"]
        self.role_field = next(
            (f for f in Agent._DAADIT_ROLE_FIELDS if f in Agent._fields), None,
        )
        if not self.role_field:
            self.skipTest("ai.agent has no role field in this database")
        self.robin = Agent.create({
            "name": "Test-Robin", "daadit_is_orchestrator": True,
        })
        self.bram = Agent.create({
            "name": "Test-Bram", self.role_field: "Testrol Rapportage",
        })

    def test_role_resolves_to_the_colleague(self):
        target, err = self.robin._daadit_resolve_named_agent(
            "testrol rapportage",
        )
        self.assertFalse(err)
        self.assertEqual(target, self.bram)

    def test_role_still_resolves_after_a_rename(self):
        self.bram.name = "Test-Bas"
        target, err = self.robin._daadit_resolve_named_agent(
            "Testrol Rapportage",
        )
        self.assertFalse(err)
        self.assertEqual(target.name, "Test-Bas")

    def test_name_wins_over_role(self):
        self.env["ai.agent"].create({
            "name": "Testrol Rapportage", self.role_field: "Iets anders",
        })
        target, _err = self.robin._daadit_resolve_named_agent(
            "Testrol Rapportage",
        )
        self.assertEqual(target.name, "Testrol Rapportage")

    def test_shared_role_is_ambiguous(self):
        self.env["ai.agent"].create({
            "name": "Test-Bea", self.role_field: "Testrol Rapportage",
        })
        target, err = self.robin._daadit_resolve_named_agent(
            "Testrol Rapportage",
        )
        self.assertFalse(target)
        self.assertIn("ambiguous", err["error"])

    def test_unknown_lists_names_with_roles(self):
        _target, err = self.robin._daadit_resolve_named_agent("Niemand-xyz")
        self.assertIn("Test-Bram (Testrol Rapportage)", err["error"])
