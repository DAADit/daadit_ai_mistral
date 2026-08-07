# -*- coding: utf-8 -*-
"""Robin as pure orchestrator: ask specialists or open a chat, never execute."""
from odoo.tests import common, tagged

from odoo.addons.daadit_ai_mistral.services import tool_dispatch as td
from odoo.addons.daadit_ai_mistral.services import llm_api_patch


@tagged("post_install", "-at_install", "daadit_ai")
class TestOrchestratorMode(common.TransactionCase):

    def setUp(self):
        super().setUp()
        Agent = self.env["ai.agent"]
        self.robin = Agent.create({
            "name": "Test-Robin",
            "daadit_is_orchestrator": True,
        })
        self.bram = Agent.create({"name": "Test-Bram"})
        self.hybrid = Agent.create({
            "name": "Test-Hybrid",
            "daadit_is_orchestrator": False,
        })

    def test_orchestrator_flag_is_readable(self):
        self.assertTrue(self.robin._daadit_orchestrator_mode())
        self.assertFalse(self.hybrid._daadit_orchestrator_mode())

    def test_orchestrator_fallback_hint_forbids_own_tools(self):
        hint = self.robin._daadit_routing_fallback_hint()
        self.assertIn("open_agent_chat", hint)
        self.assertIn("Do NOT search", hint)
        self.assertNotIn("Answer with your own tools", hint)

    def test_hybrid_fallback_hint_keeps_own_tools(self):
        self.assertEqual(
            self.hybrid._daadit_routing_fallback_hint(),
            "Answer with your own tools instead.",
        )

    def test_resolve_named_agent_exact_match(self):
        target, err = self.robin._daadit_resolve_named_agent("Test-Bram")
        self.assertFalse(err)
        self.assertEqual(target, self.bram)

    def test_resolve_named_agent_refuses_self(self):
        target, err = self.robin._daadit_resolve_named_agent("Test-Robin")
        self.assertFalse(target)
        self.assertIn("myself", err["error"])

    def test_filter_orchestrator_tools_keeps_only_router_tools(self):
        tools = td.annotate_tools([
            "ir_actions_server_search",
            "ir_actions_server_ask_agent",
            "ir_actions_server_open_agent_chat",
            "ir_actions_server_assign_user",
        ])
        kept = llm_api_patch._filter_orchestrator_tools(self.robin, tools)
        names = sorted(
            (t.get("function") or {}).get("name") for t in kept
        )
        self.assertEqual(names, sorted([
            td.ROUTER_TOOL_SLUG,
            td.OPEN_CHAT_TOOL_SLUG,
        ]))

    def test_filter_leaves_hybrid_untouched(self):
        tools = td.annotate_tools([
            "ir_actions_server_search",
            "ir_actions_server_ask_agent",
        ])
        kept = llm_api_patch._filter_orchestrator_tools(self.hybrid, tools)
        self.assertEqual(len(kept), 2)

    def test_orchestrator_prompt_is_injected_once(self):
        conv = [{"role": "user", "content": "Hoi"}]
        once = llm_api_patch._inject_orchestrator_prompt(self.robin, conv)
        self.assertEqual(once[0]["role"], "system")
        self.assertIn("orchestrator", once[0]["content"])
        twice = llm_api_patch._inject_orchestrator_prompt(self.robin, once)
        self.assertEqual(
            sum(1 for m in twice if m.get("role") == "system"), 1,
        )

    def test_open_chat_schema_is_registered(self):
        self.assertIn(td.OPEN_CHAT_TOOL_SLUG, td.TOOL_SCHEMAS)
        self.assertIn(
            td.OPEN_CHAT_TOOL_SLUG, td.ORCHESTRATOR_TOOL_SLUGS,
        )

    def test_open_agent_chat_tool_missing_name(self):
        res = self.robin._ai_tool_open_agent_chat()
        self.assertIn("error", res)
        self.assertIn("agent_name", res["error"])

    def test_open_agent_chat_tool_alias_repair(self):
        # Unknown agent → clear error that lists available names; proves
        # the alias path fed agent_name through.
        res = self.robin._ai_tool_open_agent_chat(
            specialist="Nobody-Here-XYZ",
        )
        self.assertIn("error", res)
        self.assertIn("Nobody-Here-XYZ", res["error"])

    def test_ask_agent_width_budget_uses_orchestrator_hint(self):
        td.router_state.depth = 0
        td.router_state.calls = self.robin._DAADIT_ROUTER_MAX_CALLS_PER_TURN
        try:
            res = self.robin._ai_tool_ask_agent(
                agent_name="Test-Bram", question="Hoeveel leads?",
            )
            self.assertIn("error", res)
            self.assertIn("open_agent_chat", res["error"])
            self.assertNotIn("Answer with your own tools", res["error"])
        finally:
            td.router_state.calls = 0
            td.router_state.depth = 0

    def test_channel_from_action_reads_res_id(self):
        Channel = self.env["discuss.channel"]
        # Some DBs lack ai_chat; create a plain channel and point the
        # action at it — the helper only needs an id it can browse.
        channel = Channel.create({"name": "handoff-test"})
        found = self.robin._daadit_channel_from_action({
            "type": "ir.actions.act_window",
            "res_model": "discuss.channel",
            "res_id": channel.id,
        })
        self.assertEqual(found, channel)

    def test_channel_from_action_reads_context_active_id(self):
        channel = self.env["discuss.channel"].create({"name": "handoff-ctx"})
        found = self.robin._daadit_channel_from_action({
            "type": "ir.actions.client",
            "tag": "mail.action_discuss",
            "context": {"active_id": channel.id},
        })
        self.assertEqual(found, channel)

    def test_channel_from_action_reads_discuss_channel_token(self):
        channel = self.env["discuss.channel"].create({"name": "handoff-tok"})
        found = self.robin._daadit_channel_from_action({
            "type": "ir.actions.client",
            "context": {
                "default_active_id": "discuss.channel_%s" % channel.id,
            },
        })
        self.assertEqual(found, channel)

    def test_open_chat_refuses_inside_sub_run(self):
        td.router_state.depth = 1
        try:
            res = self.robin._ai_tool_open_agent_chat(agent_name="Test-Bram")
            self.assertIn("error", res)
            self.assertIn("orchestrator", res["error"].lower())
        finally:
            td.router_state.depth = 0
