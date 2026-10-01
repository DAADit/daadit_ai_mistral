# -*- coding: utf-8 -*-
"""Een uitwijking naar Claude is nooit stil (project 91, taak 1479).

Valt Mistral uit en beantwoordt Claude de beurt, dan zet Mistral dat op
zijn router_state, zodat een geplande run het kan vastleggen. Claude's
dispatcher krijgt dezelfde collega en deadline mee, en na de beurt
staat Claude's eigen toestand weer zoals hij was.
"""
import threading
from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests import common, tagged

from ..services import llm_api_patch, tool_dispatch
from ..services.mistral_client import MistralUnavailable


@tagged("post_install", "-at_install")
class TestFallbackSignal(common.BaseCase):

    def setUp(self):
        super().setUp()
        self.claude_td = SimpleNamespace(
            current_agent=threading.local(),
            router_state=threading.local(),
        )
        self.claude_td.current_agent.record = "eigen-claude-agent"
        self.claude_td.router_state.run_deadline_monotonic = None
        self.seen = {}

        def _claude(api_self, *args, **kwargs):
            self.seen["agent"] = self.claude_td.current_agent.record
            self.seen["deadline"] = (
                self.claude_td.router_state.run_deadline_monotonic
            )
            self.seen["model"] = kwargs.get("model")
            return "antwoord van Claude"

        self.claude = SimpleNamespace(
            tool_dispatch=self.claude_td, _request_llm_claude=_claude,
        )
        for attr in ("fallback_provider", "fallback_model",
                     "fallback_reason"):
            setattr(tool_dispatch.router_state, attr, None)
        self.addCleanup(self._reset_state)

    def _reset_state(self):
        tool_dispatch.current_agent.record = None
        tool_dispatch.router_state.run_deadline_monotonic = None
        for attr in ("fallback_provider", "fallback_model",
                     "fallback_reason"):
            setattr(tool_dispatch.router_state, attr, None)

    def _turn(self, side_effect):
        tool_dispatch.current_agent.record = "bo"
        tool_dispatch.router_state.run_deadline_monotonic = 123.0
        with patch.object(llm_api_patch, "claude_patch", self.claude), \
                patch.object(llm_api_patch, "_fallback_enabled",
                             return_value=True), \
                patch.object(llm_api_patch, "_request_llm_mistral",
                             side_effect=side_effect):
            return llm_api_patch._request_with_fallback(
                SimpleNamespace(env=None), (),
                {"model": "mistral-large-latest"},
            )

    def test_uitwijking_staat_op_de_router_state(self):
        answer = self._turn(MistralUnavailable("503 van Mistral"))
        self.assertEqual(answer, "antwoord van Claude")
        state = tool_dispatch.router_state
        self.assertEqual(state.fallback_provider, "claude")
        self.assertEqual(state.fallback_model, self.seen["model"])
        self.assertIn("503", state.fallback_reason)

    def test_claude_krijgt_collega_en_deadline_en_geeft_ze_terug(self):
        self._turn(MistralUnavailable("down"))
        self.assertEqual(self.seen["agent"], "bo")
        self.assertEqual(self.seen["deadline"], 123.0)
        self.assertEqual(
            self.claude_td.current_agent.record, "eigen-claude-agent",
        )
        self.assertIsNone(self.claude_td.router_state.run_deadline_monotonic)

    def test_zonder_uitwijking_geen_signaal(self):
        answer = self._turn(lambda *a, **k: "antwoord van Mistral")
        self.assertEqual(answer, "antwoord van Mistral")
        self.assertIsNone(tool_dispatch.router_state.fallback_provider)
