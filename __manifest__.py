# -*- coding: utf-8 -*-
{
    "name": "DAADit AI — Mistral Provider",
    "summary": "Add Mistral AI as an LLM provider for Odoo's built-in AI features",
    "description": """
DAADit AI — Mistral Provider
=============================
Extends Odoo 19 Enterprise's ``ai`` module to support Mistral AI as a third
LLM provider alongside OpenAI (ChatGPT) and Google (Gemini), with feature
parity across chat, embeddings and tool calling.

Features
--------
* **Chat completions** — adds Mistral models to ``ai.agent.llm_model``
  (``mistral-large-latest``, ``mistral-medium-latest``,
  ``mistral-small-latest``, ``codestral-latest``, ``pixtral-large-latest``,
  ``ministral-8b-latest``, ``ministral-3b-latest``) and routes them to
  ``POST https://api.mistral.ai/v1/chat/completions``.
* **Tool calling** — passes ``tools`` and ``tool_choice`` through to Mistral
  when an agent has ``topic_ids`` configured; auto-builds OpenAI-compatible
  tool defs from ``ai.topic.tool_ids`` (``ir.actions.server``).
* **Embeddings** — adds ``mistral-embed`` to ``ai.embedding.embedding_model``
  and routes embedding generation to ``POST /v1/embeddings`` so Sources / RAG
  fully run on Mistral.
* **Settings UI** — adds a Mistral provider block to General Settings → AI
  next to the existing ChatGPT / Gemini blocks.

Notes
-----
The exact override points on ``ai.agent`` and ``ai.embedding`` are documented
in their respective files. Multiple candidate names are wrapped to survive
across Odoo versions; verify against the installed Enterprise source on your
Odoo.sh dev branch before relying on this in production.
""",
    "version": "19.0.4.5.0",
    "category": "Productivity/Discuss",
    "author": "DAADit",
    "website": "https://daadit.group",
    "license": "LGPL-3",
    "depends": [
        "base",
        "ai",
        "ai_app",
    ],
    "external_dependencies": {
        "python": ["requests"],
    },
    "data": [
        "security/ir.model.access.csv",
        "security/mistral_usage_security.xml",
        "data/ai_tools.xml",
        "data/cost_cap_params.xml",
        "views/res_config_settings_views.xml",
        "views/mistral_usage_views.xml",
        "views/ai_agent_views.xml",
        "views/res_partner_views.xml",
        # data/debug_actions.xml was dropped in v3.5.2 — Odoo 19's
        # safe-eval forbids IMPORT_NAME/IMPORT_FROM opcodes in server
        # action ``code`` blocks, so the introspection actions can't be
        # loaded at install time. The diagnostics module (UserError
        # trace tap + register-hook bytecode scan) covers what they did.
        #
        # data/ai_tools.xml (v19.0.4.0.0) adds two write-side AI tools
        # backed by ai.agent._ai_tool_assign_user and
        # ai.agent._ai_tool_schedule_activity. Their slugified action
        # names (ir_actions_server_assign_user / ir_actions_server_
        # schedule_activity) match the dispatch mapping in
        # services/tool_dispatch.py.
    ],
    "pre_init_hook": "pre_init_hook",
    "uninstall_hook": "uninstall_hook",
    "installable": True,
    "application": False,
    "auto_install": False,
}
