# -*- coding: utf-8 -*-
"""Tool-call execution for Mistral chat completions.

Stock Enterprise's ``ai.agent`` exposes a set of ``_ai_tool_*`` private
methods that implement the actions referenced by the topic-bound
``ir.actions.server`` records (for example "AI: Search" → calls
``record._ai_tool_search(model_name, domain, fields, offset, limit,
order)``). The OpenAI-side dispatch in stock executes these in a loop,
feeds the results back to the model, and repeats until the model
returns a final text response.

For the Mistral path we have to mirror that loop ourselves because we
hijacked ``LLMApiService.request_llm`` — stock's loop never runs for
Mistral.

This module:

* Provides ``current_agent`` — a threadlocal that the ``_get_provider``
  override on ``ai.agent`` stashes the agent record in. ``request_llm``
  picks it back up so we know which ``ai.agent`` instance to dispatch
  ``_ai_tool_*`` calls on.
* ``run_tool_call(agent, tool_call)`` — given a Mistral-formatted tool
  call dict, look up the matching ``_ai_tool_*`` method by slugified
  name, invoke it with the JSON-decoded arguments, return the result.
  Errors are converted to a JSON-encodable error string so the model
  can recover.
* ``TOOL_SCHEMAS`` — proper JSON-schema parameter definitions for the
  ten stock AI tools, replacing the empty ``{}`` schema used in
  v3.6.6–v3.6.8. With these, Mistral knows what args each tool takes.
"""
import ast
import json
import logging
import re
import threading
from datetime import date, datetime, timedelta

_logger = logging.getLogger(__name__)

# Threadlocal where the ai.agent's _get_provider override stashes the
# record so request_llm can pick it up. Cleared in a try/finally so
# leftover state doesn't leak across requests on the same worker.
current_agent = threading.local()

# Router state for the ``AI: Ask Agent`` tool (v19.0.4.2.0). ``depth``
# counts how many routed sub-runs are active on this thread; the tool
# refuses to route when depth >= 1 (Concierge → specialist only, no
# chains, no A→B→A loops) and the Mistral loop shortens its iteration
# budget for sub-runs. ``calls`` is the per-turn width budget (reset at
# each top-level turn). ``exhausted`` is set by the Mistral loop when a
# sub-run hits MAX_ITER so the router can report failure instead of
# passing off truncated narration as a real answer (v19.0.4.2.1).
router_state = threading.local()

# Tool slugs that MUTATE database state. Stripped from every routed
# sub-run's tool list (v19.0.4.2.1): routing exists to fetch an
# ANSWER, never to let a delegated agent write on the caller's behalf.
# This keeps the draft-only policy intact across the router boundary —
# e.g. routing a question to the Helpdesk SLA Agent (which owns
# Assign User / Schedule Activity) can only ever read.
WRITE_SIDE_TOOL_SLUGS = frozenset({
    "ir_actions_server_assign_user",
    "ir_actions_server_schedule_activity",
})

# The router tool itself — never exposed inside a routed sub-run
# (depth guard is defence-in-depth; this is the primary strip).
ROUTER_TOOL_SLUG = "ir_actions_server_ask_agent"


# ---------------------------------------------------------------------------
# Tool name → ai.agent method mapping
# ---------------------------------------------------------------------------
#
# Stock generates tool names by slugifying the action name and prefixing
# with the model's table name: "AI: Search" on ``ir.actions.server`` ⇒
# ``ir_actions_server_search``. Every such tool maps to a private method
# on ``ai.agent`` named ``_ai_tool_<suffix>``.
#
# We strip the prefix and prepend ``_ai_tool_`` to derive the method name.

_TOOL_PREFIX = "ir_actions_server_"
_AI_TOOL_PREFIX = "_ai_tool_"


def _tool_name_to_method(tool_name: str) -> str:
    """Map ``ir_actions_server_search`` ⇒ ``_ai_tool_search``."""
    if tool_name.startswith(_TOOL_PREFIX):
        return _AI_TOOL_PREFIX + tool_name[len(_TOOL_PREFIX):]
    # Already in ``_ai_tool_x`` form? Pass through.
    if tool_name.startswith(_AI_TOOL_PREFIX):
        return tool_name
    # Unknown — best-effort guess.
    return _AI_TOOL_PREFIX + tool_name


# Stock ai_app names a tool after its action's xml-id suffix
# (``ir_actions_server_search``) or ``action_<id>`` when the action has
# no xml-id (custom tools created in the UI).
_ACTION_ID_RE = re.compile(r"^action_(\d+)$")


def _slugify(text):
    """Lowercase, collapse non-alphanumerics to ``_``, strip edges."""
    return re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")


def _action_name_slugs(name):
    """Candidate semantic slugs for a server-action display name.

    ``"AI Marketing: write_blog (Penny)"`` ⇒ {"write_blog",
    "write_blog_penny", "ai_marketing_write_blog_penny"} — lets us also
    resolve a tool name the model *hallucinated* from the description
    when stock advertised the opaque ``action_<id>`` name.
    """
    part = name.split(":", 1)[1] if name and ":" in name else (name or "")
    slugs = {
        _slugify(name),
        _slugify(part),
        _slugify(re.sub(r"\([^)]*\)", "", part)),
    }
    slugs.discard("")
    return slugs


def _agent_tool_actions(agent):
    """The server actions this agent may invoke = ``use_in_ai``
    ``tool_ids`` across its topics, as a (sudo) recordset for metadata
    reads. Membership is read via sudo because ``tool_ids`` is a
    ``base.group_system`` field; this only decides *which* actions are
    in scope — execution privilege is unchanged, ``_ai_tool_run`` runs
    the effects as the real user."""
    if getattr(agent, "env", None) is None:
        return None
    try:
        return agent.sudo().topic_ids.tool_ids.filtered(
            lambda a: a.use_in_ai
        )
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.tool_dispatch: could not read topic "
            "tool_ids for agent=%s", getattr(agent, "id", "?"),
        )
        return None


def _resolve_tool_action(agent, fn_name):
    """Resolve a tool name to its backing ``ir.actions.server`` record,
    STRICTLY within the agent's own topic tools.

    Executing the ACTION (via stock's ``_ai_tool_run``) instead of the
    underlying ``_ai_tool_*`` method matters twice over: custom tools
    (``action_<id>``) have no backing method at all, and operators put
    guard code in the action body that must run on every dispatch path.

    Scoping to ``agent.topic_ids.tool_ids`` mirrors stock's implicit
    scoping (tools are only ever advertised from the agent's own topic
    set), so a model can never reach an action outside its advertised
    tools — not even by guessing another agent's ``action_<id>``.
    Returns a record in the agent's (non-sudo) environment, or None.
    """
    candidates = _agent_tool_actions(agent)
    if not candidates:
        return None
    fn_name = fn_name or ""
    try:
        # 1) ``action_<id>`` — stock's default advertised name.
        m = _ACTION_ID_RE.match(fn_name)
        if m:
            hit = candidates.filtered(lambda a: a.id == int(m.group(1)))
            return hit[:1].with_env(agent.env) if hit else None
        # 2) Bare XML-id suffix (stock's name when the action has one).
        ext = candidates.get_external_id()
        for act in candidates:
            xmlid = ext.get(act.id)
            if xmlid and xmlid.split(".", 1)[-1] == fn_name:
                return act.with_env(agent.env)
        # 3) Semantic slug — a name hallucinated from the description
        #    when the advertised ``action_<id>`` was opaque.
        want = _slugify(fn_name)
        if want:
            for act in candidates:
                if want in _action_name_slugs(act.name):
                    return act.with_env(agent.env)
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.tool_dispatch: action resolution raised "
            "for %r — falling back to method dispatch", fn_name,
        )
    return None


# ---------------------------------------------------------------------------
# JSON schemas for the stock AI tools
# ---------------------------------------------------------------------------
#
# Derived from the server-action ``code`` text observed on staging:
#
#   AI: Search          → record._ai_tool_search(model_name, domain,
#                          fields, offset, limit, order)
#   AI: Read group      → record._ai_tool_read_group(model_name, domain,
#                          groupby, aggregates, having, offset, limit,
#                          order)
#   AI: Get Fields      → record._ai_tool_get_fields(model_name,
#                          include_description)
#   AI: Get Menu Details→ record._ai_tool_get_menu_details(menu_ids)
#   AI: Open Menu Kanban→ record._ai_tool_open_menu_kanban(menu_id,
#                          model_name, selected_filters,
#                          selected_groupbys, search, custom_domain)
#   AI: Open Menu List  → record._ai_tool_open_menu_list(...)   (same)
#   AI: Open Menu Graph → record._ai_tool_open_menu_graph(menu_id,
#                          model_name, selected_filters,
#                          selected_groupbys, measure, mode, order,
#                          search, stacked, cumulated, custom_domain)
#   AI: Open Menu Pivot → record._ai_tool_open_menu_pivot(menu_id,
#                          model_name, selected_filters, row_groupbys,
#                          col_groupbys, measures, search, custom_domain)
#   AI: Adjust Search   → record._ai_tool_adjust_search(model_name,
#                          remove_facets, toggle_filters, toggle_groupbys,
#                          apply_searches, measures, mode, order, stacked,
#                          cumulated, custom_domain, switch_view_type)
#   AI: Compute Report Measures → record._ai_tool_compute_report_measures(
#                          action_id, model_name)


def _domain_schema():
    # Stock's _ai_tool_search has signature ``domain=''`` and runs
    # ``json.loads(domain)`` internally. So this MUST be a JSON-encoded
    # string (a string that contains a JSON array), not a JSON array.
    # Discovered from staging logs (v3.7.2):
    #   ValueError: Invalid JSON format for custom domain
    #   TypeError: the JSON object must be str, bytes or bytearray, not list
    return {
        "type": "string",
        "description": (
            "Odoo domain filter as a JSON-encoded STRING (not an array). "
            "Stock parses this with json.loads internally, so wrap your "
            "filter array in quotes and JSON-escape its contents. "
            "Examples (literal strings): "
            "no filter → \"[]\", "
            "single condition → \"[[\\\"state\\\", \\\"=\\\", \\\"posted\\\"]]\", "
            "multiple ANDed → \"[[\\\"state\\\", \\\"=\\\", \\\"posted\\\"], "
            "[\\\"amount_total\\\", \\\">\\\", 1000]]\", "
            "OR clause → \"[\\\"|\\\", [\\\"state\\\", \\\"=\\\", \\\"draft\\\"], "
            "[\\\"state\\\", \\\"=\\\", \\\"posted\\\"]]\". "
            "Operators: '=', '!=', '>', '<', '>=', '<=', 'like', 'ilike', "
            "'in', 'not in', 'child_of'. Logical: '|' for OR over the "
            "next two terms, '&' for AND (default), '!' for NOT."
        ),
        "default": "[]",
    }


def _string_array(desc, default=None):
    schema = {
        "type": "array",
        "items": {"type": "string"},
        "description": desc,
    }
    if default is not None:
        schema["default"] = default
    return schema


TOOL_SCHEMAS = {
    "ir_actions_server_search_knowledge": {
        "description": (
            "Search this agent's own knowledge sources (Odoo Knowledge "
            "articles) by meaning rather than keywords — a question "
            "about an 'invoice' also finds a passage that says 'bill'. "
            "Use it to answer from documented company knowledge instead "
            "of from memory. Pass the question as literally as you can. "
            "Returns {'ok': true, 'chunks': [{'source', 'url', "
            "'content'}]}. An EMPTY chunks list means the knowledge "
            "base holds no answer: say so, never invent one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The question to look up, in the asker's own "
                        "words where possible."
                    ),
                },
                "top_n": {
                    "type": "integer",
                    "default": 5,
                    "description": "Passages to return (1-10). Default 5.",
                },
            },
            "required": ["query"],
        },
    },
    "ir_actions_server_search": {
        "description": (
            "Search records of an Odoo model. Returns a list of dicts "
            "with the requested fields. Prefer this when answering "
            "factual questions about specific records."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_name": {
                    "type": "string",
                    "description": "Technical model name, e.g. 'res.partner', 'account.move', 'sale.order'.",
                },
                "domain": _domain_schema(),
                "fields": _string_array(
                    "Field names to read. Keep this short — 3–6 fields "
                    "is usually enough."
                ),
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 80,
                          "description": "Max records to return (cap at 200)."},
                "order": {
                    "type": "string",
                    "description": "Sort spec like 'name asc, id desc'. Optional.",
                },
            },
            "required": ["model_name"],
        },
    },
    "ir_actions_server_read_group": {
        "description": (
            "Aggregate (count, sum, avg) records of a model, grouped "
            "by one or more fields. Use this for questions like "
            "'how many invoices', 'total revenue per month', "
            "'top customers by sales'. "
            "For a simple total count with NO grouping, pass "
            "groupby=[] and aggregates=['__count']."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_name": {"type": "string",
                    "description": "Technical model name. Common ones: "
                    "'account.move' (invoices/bills), 'sale.order', "
                    "'purchase.order', 'product.template', 'res.partner', "
                    "'stock.picking', 'crm.lead', 'project.task'."},
                "domain": _domain_schema(),
                "groupby": _string_array(
                    "JSON array of field names to group by. "
                    "Empty array [] = no grouping (single-row result). "
                    "Date fields can have an interval suffix: "
                    "['create_date:month'], ['create_date:year']. "
                    "Examples: [], ['state'], ['partner_id'], "
                    "['create_date:month']."
                ),
                "aggregates": _string_array(
                    "JSON array of aggregations as 'field:operator' "
                    "strings, except '__count' which counts records. "
                    "Operators: 'sum', 'avg', 'min', 'max', "
                    "'count_distinct'. "
                    "Examples: ['__count'], ['amount_total:sum'], "
                    "['amount_total:avg', '__count'], "
                    "['partner_id:count_distinct']."
                ),
                "having": _domain_schema(),
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 80},
                "order": {"type": "string",
                    "description": "Sort spec on aggregates, e.g. "
                    "'amount_total desc' to find biggest values first."},
            },
            "required": ["model_name", "groupby", "aggregates"],
        },
    },
    "ir_actions_server_get_fields": {
        "description": (
            "Introspect a model: return the list of available fields "
            "with their types. Use this if you need to know what fields "
            "exist before searching or grouping."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_name": {"type": "string"},
                "include_description": {"type": "boolean", "default": False},
            },
            "required": ["model_name"],
        },
    },
    "ir_actions_server_get_menu_details": {
        "description": "Return menu metadata (model, default views, etc.) for one or more menu IDs.",
        "parameters": {
            "type": "object",
            "properties": {
                "menu_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of menu IDs to inspect.",
                },
            },
            "required": ["menu_ids"],
        },
    },
    "ir_actions_server_open_menu_kanban": {
        "description": "Open a menu's kanban view in the UI with optional filters.",
        "parameters": {
            "type": "object",
            "properties": {
                "menu_id": {"type": "integer"},
                "model_name": {"type": "string"},
                "selected_filters": _string_array("Names of search filters to apply.", default=[]),
                "selected_groupbys": _string_array("Names of group-by fields to apply.", default=[]),
                "search": {"type": "string", "description": "Free-text search.", "default": ""},
                "custom_domain": _domain_schema(),
            },
            "required": ["menu_id", "model_name"],
        },
    },
    "ir_actions_server_open_menu_list": {
        "description": "Open a menu's list view in the UI with optional filters.",
        "parameters": {
            "type": "object",
            "properties": {
                "menu_id": {"type": "integer"},
                "model_name": {"type": "string"},
                "selected_filters": _string_array("Names of search filters to apply.", default=[]),
                "selected_groupbys": _string_array("Names of group-by fields to apply.", default=[]),
                "search": {"type": "string", "description": "Free-text search.", "default": ""},
                "custom_domain": _domain_schema(),
            },
            "required": ["menu_id", "model_name"],
        },
    },
    "ir_actions_server_open_menu_graph": {
        "description": "Open a menu's graph view (bar/line/pie) with grouping and measure config.",
        "parameters": {
            "type": "object",
            "properties": {
                "menu_id": {"type": "integer"},
                "model_name": {"type": "string"},
                "selected_filters": _string_array("Names of search filters to apply.", default=[]),
                "selected_groupbys": _string_array("Names of group-by fields to apply.", default=[]),
                "measure": {"type": "string", "description": "Measure to plot, e.g. 'amount_total:sum'."},
                "mode": {"type": "string", "enum": ["bar", "line", "pie"], "default": "bar"},
                "order": {"type": "string", "default": ""},
                "search": {"type": "string", "default": ""},
                "stacked": {"type": "boolean", "default": False},
                "cumulated": {"type": "boolean", "default": False},
                "custom_domain": _domain_schema(),
            },
            "required": ["menu_id", "model_name"],
        },
    },
    "ir_actions_server_open_menu_pivot": {
        "description": "Open a menu's pivot view (cross-tab) with row/column groupings and measures.",
        "parameters": {
            "type": "object",
            "properties": {
                "menu_id": {"type": "integer"},
                "model_name": {"type": "string"},
                "selected_filters": _string_array("Filters to apply.", default=[]),
                "row_groupbys": _string_array("Row group-by fields.", default=[]),
                "col_groupbys": _string_array("Column group-by fields.", default=[]),
                "measures": _string_array("Measures to compute, e.g. ['amount_total:sum'].", default=[]),
                "search": {"type": "string", "default": ""},
                "custom_domain": _domain_schema(),
            },
            "required": ["menu_id", "model_name"],
        },
    },
    "ir_actions_server_adjust_search": {
        "description": (
            "Adjust filters / groupings / measures of the currently-open view. "
            "Use only when the user is already looking at a view and asks to "
            "filter, sort, group, or change the chart settings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_name": {"type": "string"},
                "remove_facets": _string_array("Active facets to remove.", default=[]),
                "toggle_filters": _string_array("Filters to toggle.", default=[]),
                "toggle_groupbys": _string_array("Groupbys to toggle.", default=[]),
                "apply_searches": _string_array("Free-text searches to add.", default=[]),
                "measures": _string_array("Measures to apply.", default=[]),
                "mode": {"type": "string", "default": ""},
                "order": {"type": "string", "default": ""},
                "stacked": {"type": "boolean", "default": False},
                "cumulated": {"type": "boolean", "default": False},
                "custom_domain": _domain_schema(),
                "switch_view_type": {"type": "string", "description": "Switch to 'list', 'kanban', 'graph', 'pivot', etc.", "default": ""},
            },
            "required": ["model_name"],
        },
    },
    "ir_actions_server_compute_report_measures": {
        "description": "Get the list of available measures for a report action (used before plotting).",
        "parameters": {
            "type": "object",
            "properties": {
                "action_id": {"type": "integer"},
                "model_name": {"type": "string"},
            },
            "required": ["action_id", "model_name"],
        },
    },
    # --- DAADit router tool (v19.0.4.2.0) -------------------------------
    "ir_actions_server_ask_agent": {
        "description": (
            "ROUTER TOOL — delegate a question to a specialist AI agent "
            "by name and return its answer. Use this FIRST for any "
            "domain question that matches a specialist (e.g. 'Sales "
            "Agent' for pipeline/leads/quotes, 'Project Agent' for "
            "projects/tasks/hours, 'Product Agent' for products/owners, "
            "'Marketing Agent' for lead sources/campaigns, 'Helpdesk "
            "SLA Agent' for tickets/SLA). Formulate the question "
            "self-contained (the specialist does not see this "
            "conversation). If the result contains 'error', answer the "
            "question yourself with your other tools instead and briefly "
            "mention that the specialist could not be reached; do not "
            "show the raw error text, and do not retry the same failing "
            "route this turn. Route at most a few times per turn."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": (
                        "Exact display name of the target agent, e.g. "
                        "'Sales Agent', 'Project Agent', 'Product "
                        "Agent', 'Marketing Agent', 'Helpdesk SLA "
                        "Agent'."
                    ),
                },
                "question": {
                    "type": "string",
                    "description": (
                        "The full, self-contained question for the "
                        "specialist, in the user's language. Include "
                        "all relevant context (time ranges, names, "
                        "filters) — the specialist cannot see this "
                        "conversation."
                    ),
                },
            },
            "required": ["agent_name", "question"],
        },
    },
    # --- DAADit write-side tools (v19.0.4.0.0) --------------------------
    "ir_actions_server_assign_user": {
        "description": (
            "WRITE TOOL — assign a user to a record by setting its "
            "'user_id' field. Use this when an agent must take ownership "
            "of a record (e.g. assign an unassigned helpdesk ticket to "
            "the right salesperson). Target model must have a 'user_id' "
            "many2one to res.users; assignee must be an active internal "
            "user. Returns {'ok': true, ...} on success, {'error': '...'} "
            "on validation failure, or {'ok': true, 'skipped': true, "
            "'reason': 'already_assigned'} if the user was already set."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_name": {
                    "type": "string",
                    "description": (
                        "Technical model name of the record, e.g. "
                        "'helpdesk.ticket', 'crm.lead', 'sale.order'."
                    ),
                },
                "record_id": {
                    "type": "integer",
                    "description": "Database id of the record to assign.",
                },
                "user_id": {
                    "type": "integer",
                    "description": (
                        "Database id of the res.users record to set as "
                        "'user_id'. Must be an active internal user."
                    ),
                },
            },
            "required": ["model_name", "record_id", "user_id"],
        },
    },
    "ir_actions_server_schedule_activity": {
        "description": (
            "WRITE TOOL — schedule a mail.activity on a record (e.g. "
            "'follow up on overdue ticket'). Idempotent: if an open "
            "activity with the same type, summary and assignee already "
            "exists on the record, no new one is created. Target model "
            "must inherit mail.activity.mixin (most business models do). "
            "Returns {'ok': true, 'activity_id': N, ...} on success, "
            "{'ok': true, 'skipped': true, 'reason': 'duplicate', "
            "'existing_activity_id': N} when an equivalent activity "
            "already exists, or {'error': '...'} on validation failure."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_name": {
                    "type": "string",
                    "description": (
                        "Technical model name of the record, e.g. "
                        "'helpdesk.ticket', 'crm.lead', 'sale.order'."
                    ),
                },
                "record_id": {
                    "type": "integer",
                    "description": "Database id of the record.",
                },
                "activity_type_xmlid": {
                    "type": "string",
                    "description": (
                        "XML id of the mail.activity.type, e.g. "
                        "'mail.mail_activity_data_todo' (To Do), "
                        "'mail.mail_activity_data_call' (Call), "
                        "'mail.mail_activity_data_email' (Email). "
                        "Optional — defaults to 'mail.mail_activity_data_todo'."
                    ),
                },
                "activity_type_id": {
                    "type": "integer",
                    "description": (
                        "Database id of the mail.activity.type. "
                        "Alternative to activity_type_xmlid; if both "
                        "given, the id wins."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "Short title for the activity (e.g. 'SLA "
                        "verstreken — direct oppakken'). Used for "
                        "idempotence: an existing activity with the "
                        "same summary + type + assignee won't be "
                        "duplicated."
                    ),
                },
                "note": {
                    "type": "string",
                    "description": (
                        "HTML body of the activity. Optional; defaults "
                        "to empty."
                    ),
                },
                "date_deadline": {
                    "type": "string",
                    "description": (
                        "Deadline as 'YYYY-MM-DD'. Optional; defaults "
                        "to today."
                    ),
                },
                "user_id": {
                    "type": "integer",
                    "description": (
                        "Database id of the user the activity is "
                        "assigned to. Optional; defaults to the "
                        "record's existing user_id if set, otherwise "
                        "the calling user."
                    ),
                },
            },
            "required": ["model_name", "record_id"],
        },
    },
}


def _custom_tool_definition(agent, name):
    """Build a tool definition for an operator-made server action from
    the action's own ``ai_tool_description`` / ``ai_tool_schema``.

    Stock stores a real JSON schema on every AI-enabled server action,
    but only advertises the tool NAME on the Mistral branch. Without the
    schema the model has no idea an ``action_1226`` takes a
    ``topic_hint``, calls it with ``{}`` and stock's executor answers
    with a ValidationError ("Could you please provide info about
    'topic_hint'") — observed in prod for every custom tool call ever
    made. Returns None when the action can't be resolved or carries no
    usable schema, so the caller falls back to the stub definition.
    """
    if agent is None:
        return None
    try:
        action = _resolve_tool_action(agent, name)
        if action is None:
            return None
        action = action.sudo()
        schema = json.loads(action.ai_tool_schema or "null")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return None
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": (
                    action.ai_tool_description
                    or action.name
                    or name.replace("_", " ").strip()
                ),
                "parameters": schema,
            },
        }
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.tool_dispatch: could not build a schema "
            "for custom tool %r — advertising it without parameters",
            name,
        )
        return None


def annotate_tools(tool_names, agent=None):
    """Given a list of tool name strings, return Mistral tool definitions
    backed by ``TOOL_SCHEMAS`` for known names, the action's own
    ``ai_tool_schema`` for operator-made tools, and a stub schema for
    anything that resolves to neither.
    """
    out = []
    for name in tool_names or []:
        if not isinstance(name, str):
            continue
        schema = TOOL_SCHEMAS.get(name)
        if schema is not None:
            out.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": schema["description"],
                    "parameters": schema["parameters"],
                },
            })
            continue
        custom = _custom_tool_definition(agent, name)
        if custom is not None:
            out.append(custom)
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": name.replace("_", " ").strip(),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        })
    return out


# ---------------------------------------------------------------------------
# Tool-call execution
# ---------------------------------------------------------------------------


def _safe_jsonable(value, depth=0):
    """Coerce arbitrary Python values to something json-encodable so we
    can stuff a tool result into the conversation as a 'tool' message."""
    if depth > 6:
        return f"<truncated {type(value).__name__}>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe_jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_jsonable(v, depth + 1) for v in value]
    if hasattr(value, "_name") and hasattr(value, "ids"):
        # Odoo recordset
        try:
            return {
                "_model": getattr(value, "_name", None),
                "ids": list(value.ids),
            }
        except Exception:  # noqa: BLE001
            return f"<recordset {value!r}>"
    return str(value)


# ---------------------------------------------------------------------------
# Tool-result size cap + binary-field strip (introduced v19.0.3.13.2)
# ---------------------------------------------------------------------------
#
# Without these, a single tool call that returns large records (most
# commonly ``ir_actions_server_search`` on ``product.product`` when the
# model omitted the ``fields`` argument and Odoo returned every column
# including ``image_1920`` as base64) can stuff hundreds of thousands of
# tokens into the next message and trigger Mistral's HTTP 400
# "prompt too large for model with 131072 maximum context length".
#
# Three layers, in order of activation:
#
#   1. Default ``fields=['id', 'display_name']`` for search calls when
#      the LLM omits ``fields`` (see ``run_tool_call``). Prevents the
#      problem at the source by making "give me ALL columns" the
#      explicit opt-in instead of the default.
#   2. Binary-field strip — recursively drops keys that conventionally
#      hold base64/raw bytes (``image_<n>``, ``avatar_<n>``, ``datas``,
#      ``raw``, ``db_datas``, ``*_binary``). Those bytes are useless in
#      chat context and routinely 50-500KB per record.
#   3. Hard size cap — after strip, if the JSON-serialised result still
#      exceeds ``_MAX_TOOL_RESULT_CHARS`` we replace it with an error
#      dict that explains how to scope down. The LLM can re-call.

# Cap is in CHARACTERS (cheap to measure). 50_000 chars is roughly
# 12-15k tokens for typical English/Dutch JSON — well below the 131k
# Mistral medium context, leaving room for system prompt, conversation
# history, and additional tool calls in the same turn.
_MAX_TOOL_RESULT_CHARS = 50_000


def _is_binary_key(key):
    """True iff ``key`` matches a known Odoo binary/base64 field name.

    Matches:
      * Exact: ``image``, ``avatar``, ``datas``, ``raw``, ``db_datas``
      * Sized: ``image_128`` / ``image_256`` / ``image_512`` /
        ``image_1024`` / ``image_1920`` (and same for ``avatar_``)
      * Suffix: any key ending in ``_binary`` (covers custom modules)
    """
    if not isinstance(key, str):
        return False
    if key in {"image", "avatar", "datas", "raw", "db_datas"}:
        return True
    if key.endswith("_binary"):
        return True
    parts = key.split("_", 1)
    if (
        len(parts) == 2
        and parts[0] in {"image", "avatar"}
        and parts[1].isdigit()
    ):
        return True
    return False


def _strip_binary_fields(value):
    """Recursively drop binary-keyed entries from a JSON-safe value.

    Operates on the output of ``_safe_jsonable`` — values are already
    plain dicts / lists / primitives, so no exotic recursion guards
    needed. Returns a new structure; does not mutate input.
    """
    if isinstance(value, dict):
        return {
            k: _strip_binary_fields(v)
            for k, v in value.items()
            if not _is_binary_key(k)
        }
    if isinstance(value, list):
        return [_strip_binary_fields(v) for v in value]
    return value


def _enforce_result_size_cap(safe, fn_name):
    """If ``safe`` (a JSON-safe value) is too large, return an error
    dict instructing the LLM to scope down. Otherwise return ``safe``
    unchanged.

    Sized in characters of the JSON encoding — measuring tokens would
    require the model's tokenizer, and char-count is a good-enough
    proxy at this scale (Mistral / OpenAI tokenisers run ~3-4 chars
    per token for mixed JSON content).
    """
    try:
        serialized = json.dumps(safe, default=str)
    except Exception:  # noqa: BLE001
        serialized = str(safe)
    n = len(serialized)
    if n <= _MAX_TOOL_RESULT_CHARS:
        return safe
    _logger.warning(
        "daadit_ai_mistral.tool_dispatch: result for %s exceeded cap "
        "(%d chars > %d) — returning error to LLM",
        fn_name, n, _MAX_TOOL_RESULT_CHARS,
    )
    return {
        "error": (
            f"Tool result too large ({n} chars; cap is "
            f"{_MAX_TOOL_RESULT_CHARS}). Re-call with a narrower scope: "
            f"pass an explicit short `fields` list (e.g. "
            f"['id', 'name', 'list_price']), tighten the `domain`, or "
            f"lower `limit`. Binary fields (image_*, datas, raw) are "
            f"already stripped server-side, so over-cap means the "
            f"non-binary content alone is too big."
        ),
        "tool_name": fn_name,
        "result_chars": n,
        "cap_chars": _MAX_TOOL_RESULT_CHARS,
    }


# Stock methods whose parameter is a JSON-string (stock json.loads-es it
# internally). Discovered from staging logs (v3.7.2):
#   _ai_tool_search(model_name, domain='', ...)
#   _ai_tool_*(... custom_domain='', ...)
# Mistral naturally sends these as arrays — we encode them to strings
# before calling the method.
_JSON_STRING_PARAMS = ("domain", "having", "custom_domain")

# Wrapper-keys Mistral sometimes puts the real arguments inside —
# observed on staging:
#   {"params": {"model": "account.move", "domain": [...], ...}}
# We unwrap when the kwargs dict has exactly one key from this set
# AND its value is itself a dict.
_WRAPPER_KEYS = ("params", "body", "payload", "data", "arguments",
                 "input", "request", "kwargs")

# Parameter-name aliases. Mistral picks the most "natural" name from
# the tool description and sometimes that doesn't match the stock
# Python argument name. Map them here so the call survives.
_PARAM_ALIASES = {
    "model": "model_name",          # Mistral's preferred name
    "model_id": "model_name",
    "model_technical_name": "model_name",
    "ir_model": "model_name",
    "modelName": "model_name",
    # read_group: prod logs (2026-07-03, #87) show Mistral emitting the
    # snake-split spelling `group_by` — stock's kwarg is `groupby`.
    "group_by": "groupby",
    "groupBy": "groupby",
    "group_bys": "groupby",
    "groupbys": "groupby",
    "aggregate": "aggregates",
}


# read_group aggregate spellings Mistral actually emits versus what
# Odoo's ORM accepts. Prod logs (2026-07-03, #88/#89): `count` →
# "Invalid field 'count'", `id` → "Aggregate method is mandatory for
# 'id'". The ORM's record-count aggregate is the literal `__count`.
_COUNT_AGGREGATE_SPELLINGS = frozenset({
    "count", "id", "*", "count(*)", "count_all", "__count__",
    "id:count", "count:count", "total", "count()",
})


# ---------------------------------------------------------------------------
# Domain normalisation (v19.0.3.13.4)
# ---------------------------------------------------------------------------
#
# Stock ``_ai_tool_search`` / ``_ai_tool_read_group`` run ``json.loads``
# on the ``domain`` / ``having`` / ``custom_domain`` string internally.
# Mistral does NOT reliably emit JSON for these — staging logs (v13.2/13.3)
# showed it sending, repeatedly:
#
#   [('stage_id.is_close', '=', False)]            ← Python tuples + single
#                                                     quotes → JSONDecodeError
#   ['sla_deadline', '<', 'datetime.now() + timedelta(days=1)']
#                                                  ← Python date expression
#                                                     as a domain value →
#                                                     Odoo "Invalid term"
#
# Both make every search fail until the model gives up (MAX_ITER). We
# repair these BEFORE handing the string to stock:
#
#   * Python-literal domains (single quotes, tuples, True/False/None)
#     are parsed with ``ast.literal_eval`` and re-encoded as JSON.
#   * Relative-date string leaves are evaluated against a tiny safe
#     whitelist (``datetime``/``date``/``timedelta``) to a concrete
#     ``YYYY-MM-DD[ HH:MM:SS]`` so Odoo's domain parser accepts them.
#
# Everything is best-effort: if a value can't be parsed we return it
# unchanged so behaviour never regresses versus no normalisation.

_REL_DATE_TOKEN_RE = re.compile(
    r"\b(datetime|date|timedelta|relativedelta|fields|now|today)\b"
)

# Names allowed inside a relative-date expression after normalisation.
_REL_DATE_ALLOWED_NAMES = frozenset(
    {"datetime", "date", "timedelta", "now", "today"}
)


def _eval_relative_date(expr):
    """Best-effort: evaluate a relative-date Python expression to a
    concrete Odoo-compatible string. Returns ``None`` when ``expr`` is
    not a recognised, safe date expression.

    Recognises the spellings the LLM actually emits, e.g.
    ``datetime.now() + timedelta(days=1)``, ``date.today()``,
    ``fields.Datetime.now()``, bare ``today`` / ``now``.
    """
    if not isinstance(expr, str):
        return None
    s = expr.strip()
    if not s or not _REL_DATE_TOKEN_RE.search(s):
        return None
    norm = (
        s.replace("fields.Datetime.now()", "datetime.now()")
         .replace("fields.Datetime.today()", "datetime.now()")
         .replace("fields.Date.today()", "date.today()")
         .replace("fields.Date.context_today()", "date.today()")
         .replace("datetime.datetime.now()", "datetime.now()")
         .replace("datetime.date.today()", "date.today()")
    )
    low = norm.lower()
    if low in ("now", "now()"):
        norm = "datetime.now()"
    elif low == "today":
        norm = "date.today()"
    try:
        code = compile(norm, "<reldate>", "eval")
    except SyntaxError:
        return None
    # SECURITY: the co_names whitelist below is the boundary. The
    # expression may ONLY reference these five names — anything else
    # (``__import__``, ``eval``, ``os``, ``__class__``, attribute
    # walks, …) shows up in ``co_names`` and is rejected here, before
    # eval runs. With that guard in place it is safe to let Python
    # inject the real builtins (needed because CPython's
    # ``date.today()`` does an internal import and fails under an
    # empty ``__builtins__``).
    for name in code.co_names:
        if name not in _REL_DATE_ALLOWED_NAMES:
            return None
    try:
        value = eval(  # noqa: S307 - co_names allowlist is the sandbox
            code,
            {},
            {"datetime": datetime, "date": date, "timedelta": timedelta},
        )
    except Exception:  # noqa: BLE001
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return None


def _coerce_domain_obj(obj):
    """Recursively normalise a parsed domain: tuples → lists, and
    relative-date string leaves → concrete timestamps. Returns a
    JSON-safe structure."""
    if isinstance(obj, (list, tuple)):
        return [_coerce_domain_obj(x) for x in obj]
    if isinstance(obj, str):
        rel = _eval_relative_date(obj)
        return rel if rel is not None else obj
    return obj


def _normalize_json_string_param(v):
    """Return a JSON-encoded STRING for the domain/having/custom_domain
    params, which stock parses with ``json.loads`` internally.

    Accepts and repairs:
      * list / dict                 → json.dumps (after reldate coercion)
      * JSON string                 → canonical re-encode (+ reldate)
      * Python-literal string        → ast.literal_eval → json.dumps
        (single quotes, tuples,       (this is the case that was failing
        True/False/None)              in production)
      * '' / None                   → '[]'

    Falls back to the original value if nothing parses, so this can
    only ever make a previously-failing call succeed, never the
    reverse.
    """
    if v is None or v == "":
        return "[]"
    obj = None
    if isinstance(v, (list, dict)):
        obj = v
    elif isinstance(v, str):
        s = v.strip()
        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            try:
                obj = ast.literal_eval(s)
            except (ValueError, TypeError, SyntaxError):
                obj = None
    if obj is None:
        # Structurally unparseable. Last resort: a bare relative-date
        # expression passed as the whole value.
        if isinstance(v, str):
            rel = _eval_relative_date(v)
            if rel is not None:
                return json.dumps(rel)
            return v
        return "[]"
    try:
        return json.dumps(_coerce_domain_obj(obj))
    except Exception:  # noqa: BLE001
        return "[]"


# ---------------------------------------------------------------------------
# Domain post-processing for search / read_group (v19.0.4.8.0)
#
# Two Fase-0 items that both operate on the parsed domain right before
# dispatch:
#
# 1. Non-searchable fields (gate: tool-laag). Agents keep building
#    domains on computed, non-stored fields without a search method
#    (observed repeatedly: ``mail.activity.state``). The ORM refuses
#    those outright and the whole tool call dies. Instead we TRANSLATE
#    the ones we know (activity ``state`` → the ``date_deadline``
#    comparison it is computed from) and STRIP the rest (replaced by a
#    true-leaf so AND/OR structure stays intact — the result set can
#    only widen, never silently shrink), with a note in the tool result
#    so the model knows to post-filter.
#
# 2. Hard per-agent read scope (gate: hard lees-domein). When the agent
#    has a ``daadit.ai.agent.read.scope`` line for the model, that
#    domain is AND-ed in — enforced here in the tool layer, below the
#    prompt. Fail-closed: an unusable scope domain blocks the read.
# ---------------------------------------------------------------------------
# odoo.osv.expression is deprecated in 19 and warns at import (builds
# must stay warning-free). odoo.fields.Domain is the 19-native
# equivalent; ``list(Domain)`` yields the classic domain list, which
# json.dumps serialises fine.
from odoo.fields import Domain as _Domain

_TRUE_LEAF = (1, "=", 1)


def _domain_and(domains):
    return list(_Domain.AND(domains))


def _domain_or(domains):
    return list(_Domain.OR(domains))

_DOMAIN_TOOLS = ("ir_actions_server_search", "ir_actions_server_read_group")


def _translate_activity_state_leaf(env, leaf):
    """Rewrite a ``mail.activity.state`` condition to ``date_deadline``.

    ``state`` on ``mail.activity`` is computed from ``date_deadline``
    vs. the user's today and is not searchable. The mapping below is
    exactly the compute's definition, so the translation is lossless:
    overdue < today, today = today, planned > today.

    Returns a normalized domain (one expression) or None when the
    operator/value shape isn't translatable.
    """
    from odoo import fields as odoo_fields
    _f, op, value = leaf
    today = odoo_fields.Date.to_string(
        odoo_fields.Date.context_today(env.user)
    )
    direct = {
        "overdue": ("date_deadline", "<", today),
        "today": ("date_deadline", "=", today),
        "planned": ("date_deadline", ">", today),
    }
    complement = {
        "overdue": ("date_deadline", ">=", today),
        "today": ("date_deadline", "!=", today),
        "planned": ("date_deadline", "<=", today),
    }
    table = direct if op in ("=", "in") else (
        complement if op in ("!=", "not in") else None
    )
    if table is None:
        return None
    values = value if isinstance(value, (list, tuple)) else [value]
    parts = [[list(table[v])] for v in values if v in table]
    if not parts or len(parts) != len(values):
        return None
    combine = _domain_or if table is direct else _domain_and
    return combine(parts)


# (model_name, field_path) → callable(env, leaf) returning a
# replacement domain, or None to fall back to stripping the leaf.
_UNSEARCHABLE_TRANSLATORS = {
    ("mail.activity", "state"): _translate_activity_state_leaf,
}


def _domain_field_unsearchable(env, model_name, path):
    """True when ``path`` ends on a field the ORM cannot search.

    Non-stored fields without a ``search=`` method (and not related)
    raise on search. Unknown fields/models return False — the ORM's
    own "Invalid field" error is more instructive there.
    """
    try:
        current = env[model_name]
    except KeyError:
        return False
    segments = str(path).split(".")
    for i, seg in enumerate(segments):
        field = current._fields.get(seg)
        if field is None:
            return False
        if i < len(segments) - 1:
            if not field.relational:
                return False
            try:
                current = env[field.comodel_name]
            except KeyError:
                return False
            continue
        return not (field.store or field.search or field.related)
    return False


def _sanitize_unsearchable_domain(env, model_name, domain):
    """Translate or strip domain leaves on non-searchable fields.

    Returns ``(new_domain, notes)``. Stripped leaves become a
    true-leaf so prefix operators keep their arity; each rewrite adds
    a human-readable note that is attached to the tool result.
    """
    notes = []
    out = []
    for token in domain:
        is_leaf = (
            isinstance(token, (list, tuple))
            and len(token) == 3
            and isinstance(token[0], str)
        )
        if is_leaf and _domain_field_unsearchable(
            env, model_name, token[0]
        ):
            translator = _UNSEARCHABLE_TRANSLATORS.get(
                (model_name, token[0])
            )
            replacement = None
            if translator is not None:
                try:
                    replacement = translator(env, token)
                except Exception:  # noqa: BLE001
                    _logger.exception(
                        "daadit_ai_mistral.tool_dispatch: translator "
                        "for %s.%s raised — falling back to strip",
                        model_name, token[0],
                    )
            if replacement:
                out.extend(replacement)
                notes.append(
                    "Condition on '%s' (not searchable on %s) was "
                    "translated to an equivalent stored-field filter."
                    % (token[0], model_name)
                )
            else:
                out.append(list(_TRUE_LEAF))
                notes.append(
                    "Condition on '%s' was REMOVED: the field is not "
                    "searchable on %s. The result set may be wider "
                    "than requested — filter the returned records "
                    "yourself if needed." % (token[0], model_name)
                )
            continue
        out.append(token)
    return out, notes


# Tools whose stock method takes a required ``model_name`` first arg.
# Mistral periodically omits it (staging logs: repeated
# "_ai_tool_search() missing 1 required positional argument:
# 'model_name'"), burning tool-loop iterations on a TypeError. We
# pre-validate and return a crisp instruction instead.
_MODEL_REQUIRED_TOOLS = frozenset({
    "ir_actions_server_search",
    "ir_actions_server_read_group",
    "ir_actions_server_get_fields",
    "ir_actions_server_compute_report_measures",
    "ir_actions_server_open_menu_kanban",
    "ir_actions_server_open_menu_list",
    "ir_actions_server_open_menu_graph",
    "ir_actions_server_open_menu_pivot",
    "ir_actions_server_adjust_search",
    # DAADit write-side tools (v19.0.4.0.0) — both take a model_name
    # so the agent's allow/block list is enforced by tool_dispatch
    # before _ai_tool_* runs.
    "ir_actions_server_assign_user",
    "ir_actions_server_schedule_activity",
})


def _coerce_args(raw_args):
    """Parse Mistral's tool-call ``arguments`` field into a kwargs dict.

    Coercions, in order:

    1. **Top-level JSON parse.** Mistral returns ``arguments`` as a JSON
       string — we json.loads it once.

    2. **Wrapper unwrap.** Sometimes Mistral wraps the real arguments
       under a single key like ``{"params": {…}}`` or ``{"body": {…}}``.
       Detected on staging::

           {"params": {"model": "account.move",
                       "domain": [["move_type","=","out_invoice"]], …}}

       If the dict has exactly one key from ``_WRAPPER_KEYS`` and its
       value is itself a dict, we unwrap one level. Recursively, in case
       the model double-wraps.

    3. **Per-value JSON re-decode.** Mistral sometimes stringifies
       arrays/objects when its first attempt failed (a defensive habit).
       For each string value beginning with ``[`` or ``{``, try
       ``json.loads`` so a domain like
       ``"[[\\"state\\",\\"=\\",\\"posted\\"]]"`` becomes a real list.

    4. **Stock JSON-string expectations.** For parameter names in
       ``_JSON_STRING_PARAMS`` (``domain``, ``having``,
       ``custom_domain``), stock's own implementation expects a
       JSON-encoded STRING. We re-encode lists/dicts back to strings so
       stock's internal ``json.loads`` succeeds.

    5. **Parameter-name aliasing.** Map common Mistral choices like
       ``model`` → ``model_name`` so the dispatched method's signature
       is satisfied.
    """
    if isinstance(raw_args, dict):
        kwargs = dict(raw_args)
    elif isinstance(raw_args, str):
        kwargs = json.loads(raw_args) if raw_args.strip() else {}
    else:
        kwargs = dict(raw_args)

    if not isinstance(kwargs, dict):
        return kwargs

    # --- 2. Unwrap single-key wrappers (recursively, max 3 levels) ---
    for _ in range(3):
        if (
            len(kwargs) == 1
            and next(iter(kwargs)) in _WRAPPER_KEYS
            and isinstance(next(iter(kwargs.values())), dict)
        ):
            kwargs = dict(next(iter(kwargs.values())))
        else:
            break

    # --- 3 + 4 + 5 -----------------------------------------------------
    out = {}
    for k, v in kwargs.items():
        # 5. Apply parameter-name aliases.
        target_key = _PARAM_ALIASES.get(k, k)

        # 3. Best-effort decode JSON-shaped strings.
        if isinstance(v, str) and v and v[0] in "[{":
            try:
                v = json.loads(v)
            except (ValueError, TypeError):
                pass

        # 4. For parameters that stock expects as JSON-strings,
        # normalise + re-encode. Handles Python-literal domains
        # (single quotes / tuples) and relative-date expressions that
        # stock's internal json.loads + Odoo's domain parser reject.
        if target_key in _JSON_STRING_PARAMS:
            v = _normalize_json_string_param(v)

        # If aliasing collides with an explicit key already present,
        # prefer the explicit one (don't overwrite).
        if target_key in out and target_key != k:
            continue
        out[target_key] = v
    return out


def _build_signature_hint(method, fn_name):
    """Return a one-line summary of the method's signature so the model
    can self-correct on TypeError. Drops ``self``; shows defaults."""
    try:
        import inspect
        sig = inspect.signature(method)
        # method is bound (agent._ai_tool_xxx) so self is already
        # excluded by inspect.signature on a bound method.
        return f"Expected signature: {fn_name}{sig}"
    except (TypeError, ValueError):
        return ""


_LOG_TOOL_FLAG_ICP = "daadit_ai_mistral.log_tool_results"

# SEC M2: when the PII-logging flag is on, surface a one-shot WARNING
# every time a worker handles its first tool dispatch. The warning is
# loud enough to nudge admins who flipped the flag for debugging and
# forgot to flip it back.
_LOG_TOOL_FLAG_WARNED = False


def _result_logging_enabled(env):
    """Return True iff tool-call args + results should be persisted to
    ``ir.logging``. Default False because tool args/results may contain
    customer PII (partner names, amounts, free-text fields). Operators
    who explicitly want full audit logs can flip the ICP::

        daadit_ai_mistral.log_tool_results = True

    SEC M2: when the flag is on, log a per-worker WARNING the first
    time we observe it, so admins who flipped the flag for a debug
    session can see in the logs that PII is being persisted to
    ir.logging — and remember to flip it off after they're done.
    """
    global _LOG_TOOL_FLAG_WARNED
    try:
        flag = env["ir.config_parameter"].sudo().get_param(
            _LOG_TOOL_FLAG_ICP, default="False"
        )
    except Exception:  # noqa: BLE001
        return False
    on = str(flag).strip().lower() in ("1", "true", "t", "yes", "y")
    if on and not _LOG_TOOL_FLAG_WARNED:
        _LOG_TOOL_FLAG_WARNED = True
        _logger.warning(
            "daadit_ai_mistral: PII-logging flag is ENABLED — every "
            "Mistral tool call's arguments and results (capped at "
            "1500 chars) are being persisted to ir.logging. This is "
            "intended only for short-lived debugging sessions. Set "
            "ir.config_parameter %r to False to disable.",
            _LOG_TOOL_FLAG_ICP,
        )
    return on


def _record_in_ir_logging(env, level, name, message):
    """Write a row to ``ir.logging`` so the call is visible from
    XML-RPC. Best-effort — swallow any error so a logging hiccup never
    breaks the chat flow.

    Errors (level ERROR / WARNING) are *always* logged so we can
    diagnose problems. Successful calls (level INFO) are gated behind
    ``daadit_ai_mistral.log_tool_results`` because the args/result
    payload tends to contain user data we shouldn't archive without
    consent.
    """
    if level == "INFO" and not _result_logging_enabled(env):
        return
    try:
        env["ir.logging"].sudo().create({
            "name": name,
            "type": "server",
            "level": level,
            "message": message[:8000],
            "path": "daadit_ai_mistral",
            "func": "run_tool_call",
            "line": "0",
        })
        # ir.logging writes are inside a transaction — savepoint so
        # they survive a later rollback if the chat handler aborts.
        env.cr.commit()
    except Exception:  # noqa: BLE001
        pass


def run_tool_call(agent, tool_call):
    """Execute one Mistral tool call against an ``ai.agent`` record.

    ``agent`` is the recordset on which to dispatch (singleton).
    ``tool_call`` is a dict shaped like::

        {"id": "call_…", "type": "function",
         "function": {"name": "ir_actions_server_search",
                      "arguments": "<json>"}}

    Returns a JSON-serialisable result, or ``{"error": "…"}`` on
    failure (so the conversation can continue and the model can react
    to the error message). Each call is also persisted to
    ``ir.logging`` so the args + outcome are inspectable post-hoc.
    """
    if not tool_call or not isinstance(tool_call, dict):
        return {"error": "Invalid tool_call object"}

    fn = tool_call.get("function") or {}
    fn_name = fn.get("name") or ""
    raw_args = fn.get("arguments") or "{}"
    env = getattr(agent, "env", None)

    try:
        kwargs = _coerce_args(raw_args)
    except (ValueError, TypeError) as exc:
        _logger.warning(
            "daadit_ai_mistral.tool_dispatch: could not parse arguments "
            "for %s: %s | raw=%r", fn_name, exc, str(raw_args)[:300],
        )
        if env is not None:
            _record_in_ir_logging(
                env, "WARNING", "daadit_ai_mistral.tool_dispatch",
                f"PARSE_ERROR fn={fn_name} raw={str(raw_args)[:500]} "
                f"err={exc}",
            )
        return {"error": (
            f"Could not parse tool arguments as JSON: {exc}. "
            f"Pass arguments as a JSON object whose values are typed "
            f"(arrays as JSON arrays, not strings)."
        )}

    action = _resolve_tool_action(agent, fn_name)
    method_name = _tool_name_to_method(fn_name)
    method = getattr(agent, method_name, None)
    if action is None and not callable(method):
        _logger.warning(
            "daadit_ai_mistral.tool_dispatch: unknown tool %r → %s "
            "(method not on ai.agent, no matching server action)",
            fn_name, method_name,
        )
        if env is not None:
            _record_in_ir_logging(
                env, "WARNING", "daadit_ai_mistral.tool_dispatch",
                f"UNKNOWN_TOOL fn={fn_name} method={method_name}",
            )
        return {"error": f"Unknown tool: {fn_name}"}

    if not isinstance(kwargs, dict):
        return {"error": (
            f"Tool {fn_name} arguments must be a JSON object, got "
            f"{type(kwargs).__name__}."
        )}

    # ----- Required-arg pre-validation --------------------------------
    # Return a crisp instruction instead of letting the dispatch raise
    # TypeError("missing 1 required positional argument: 'model_name'")
    # and burning a tool-loop iteration on a confusing signature error.
    if fn_name in _MODEL_REQUIRED_TOOLS and not kwargs.get("model_name"):
        _logger.info(
            "daadit_ai_mistral.tool_dispatch: %s called without "
            "model_name (args_keys=%s) — returning instruction",
            fn_name, list(kwargs.keys()),
        )
        return {"error": (
            f"Tool {fn_name} requires a 'model_name' argument: the "
            f"technical Odoo model name as a string (e.g. "
            f"'helpdesk.ticket', 'res.partner', 'sale.order'). "
            f"Re-call this tool with model_name set."
        )}

    # ----- Per-agent model access control -----------------------------
    # If the tool call carries a ``model_name`` and the agent has
    # allowed/blocked lists configured, gate it BEFORE dispatch. We
    # return a structured error to Mistral so the model can
    # re-strategize ("the agent can only access X, Y, Z — try those
    # instead") rather than treating the failure as a generic crash.
    requested_model = kwargs.get("model_name")
    if requested_model and hasattr(agent, "_daadit_is_model_allowed"):
        try:
            allowed = agent._daadit_is_model_allowed(requested_model)
        except Exception:  # noqa: BLE001
            allowed = True  # fail-open on bug in our check (don't break chat)
            _logger.exception(
                "daadit_ai_mistral.tool_dispatch: model-allow check raised "
                "for agent=%s model=%s — falling back to allow",
                agent.id, requested_model,
            )
        if not allowed:
            allowed_list = sorted(
                agent.daadit_allowed_model_ids.mapped("model")
            ) if agent.daadit_allowed_model_ids else []
            blocked_list = sorted(
                agent.daadit_blocked_model_ids.mapped("model")
            ) if agent.daadit_blocked_model_ids else []
            _logger.info(
                "daadit_ai_mistral.tool_dispatch: ACCESS_DENIED "
                "agent=%s tool=%s model=%s (allowed=%s, blocked=%s)",
                agent.id, fn_name, requested_model,
                allowed_list, blocked_list,
            )
            if env is not None:
                _record_in_ir_logging(
                    env, "WARNING", "daadit_ai_mistral.tool_dispatch",
                    f"ACCESS_DENIED fn={fn_name} model={requested_model} "
                    f"agent={agent.id} allowed={allowed_list} "
                    f"blocked={blocked_list}",
                )
            hint = ""
            if allowed_list:
                hint = (
                    f" This agent can only query the following models: "
                    f"{', '.join(allowed_list)}."
                )
            elif blocked_list and requested_model in blocked_list:
                hint = (
                    f" Model '{requested_model}' is on this agent's "
                    f"block list."
                )
            # NOTE: ``_daadit_access_denied`` is the sentinel that
            # ``llm_api_patch._request_llm_mistral`` watches for. When
            # set, the dispatch loop short-circuits and surfaces a
            # user-facing message *directly*, instead of feeding the
            # error back to Mistral (which tends to mis-paraphrase it
            # as a generic "I made a mistake" recovery — confusing for
            # the end user who actually hit an admin policy).
            return {
                "_daadit_access_denied": True,
                "tool_name": fn_name,
                "model_name": requested_model,
                "allowed_models": allowed_list,
                "blocked_models": blocked_list,
                "error": (
                    f"Access denied: agent is not permitted to query "
                    f"model '{requested_model}'.{hint}"
                ),
            }

    # ------------------------------------------------------------------
    # Field-level privacy gate (introduced in 19.0.3.10.0; expanded in
    # 19.0.3.11.0).
    # Reject domain conditions, groupby values, or aggregate clauses
    # that reference a blocked field BEFORE we spend an Odoo read on
    # them — otherwise an LLM could binary-search the blocked value
    # via filter conditions or recover it via grouped aggregation
    # (e.g. group by `vat` and look at row keys).
    # ------------------------------------------------------------------
    _model_for_priv = kwargs.get("model_name") or kwargs.get("model") or ""
    if _model_for_priv:
        # Domain check
        if kwargs.get("domain") is not None:
            try:
                bad_field = agent._daadit_domain_uses_blocked_field(
                    _model_for_priv, kwargs.get("domain"),
                )
            except Exception:  # noqa: BLE001
                bad_field = ""
            if bad_field:
                return {"error": (
                    f"Filtering on '{bad_field}' is forbidden by this "
                    f"agent's privacy policy on '{_model_for_priv}'. "
                    "That field is on the field-level blocklist — it can't "
                    "be used in domain conditions because that would let "
                    "the LLM binary-search the value through filter clauses."
                )}
        # SEC M1: groupby / aggregates check.
        # ``read_group`` lets the model recover blocked values by
        # grouping on them — the result rows are keyed by the field's
        # actual values. Block any ``groupby`` / ``having`` /
        # ``aggregates`` entry that references a blocked field, with
        # or without a ``:operator`` suffix.
        try:
            blocked_fields = agent._daadit_blocked_field_set(_model_for_priv)
        except Exception:  # noqa: BLE001
            blocked_fields = set()
        if blocked_fields:
            def _strip_suffix(s):
                # 'create_date:month' → 'create_date'; 'amount_total:sum'
                # → 'amount_total'. Handles both interval and aggregate
                # operator suffixes.
                if not isinstance(s, str):
                    return ""
                return s.split(":", 1)[0].strip()

            for arg_name in ("groupby", "aggregates"):
                values = kwargs.get(arg_name)
                if not values:
                    continue
                if isinstance(values, str):
                    values = [values]
                for v in values:
                    field_part = _strip_suffix(v)
                    if field_part in blocked_fields:
                        return {"error": (
                            f"Using '{field_part}' in {arg_name} is "
                            f"forbidden by this agent's privacy policy "
                            f"on '{_model_for_priv}'. Grouping or "
                            "aggregating on a field-level-blocked field "
                            "would let the value be reconstructed from "
                            "the result rows."
                        )}
            # ``having`` is a sub-domain over the aggregated rows — same
            # binary-search risk as ``domain``. Reuse the domain checker.
            if kwargs.get("having") is not None:
                try:
                    bad_having = agent._daadit_domain_uses_blocked_field(
                        _model_for_priv, kwargs.get("having"),
                    )
                except Exception:  # noqa: BLE001
                    bad_having = ""
                if bad_having:
                    return {"error": (
                        f"Filtering on '{bad_having}' in having is "
                        f"forbidden by this agent's privacy policy on "
                        f"'{_model_for_priv}'."
                    )}

    # ------------------------------------------------------------------
    # Default fields for search calls (v19.0.3.13.2 — fix C of three)
    # ------------------------------------------------------------------
    # When the LLM omits ``fields`` on ``ir_actions_server_search``,
    # Odoo returns EVERY field including ``image_1920`` as base64. On
    # ``product.product`` etc. that blows the next LLM message past
    # the Mistral 131k-token context window. The tool schema nudges
    # toward "3-6 fields" but doesn't enforce — we do, server-side.
    # Read-side fix (fields B + A in this commit) still apply on top
    # as defence-in-depth for tools that don't take ``fields``.
    if fn_name == "ir_actions_server_search" and not kwargs.get("fields"):
        kwargs["fields"] = ["id", "display_name"]
        _logger.info(
            "daadit_ai_mistral.tool_dispatch: injected default "
            "fields=['id','display_name'] for %s on %s "
            "(LLM omitted the fields argument)",
            fn_name, requested_model or "<unknown>",
        )

    # ------------------------------------------------------------------
    # Default domain/having when the LLM omits them (v19.0.4.1.1)
    # ------------------------------------------------------------------
    # ``_normalize_json_string_param`` repairs domains that arrive
    # empty or malformed, but it only runs for keys PRESENT in the
    # arguments. When Mistral omits ``domain`` entirely (observed on
    # prod: `search(model_name='crm.lead', fields=[...])` with no
    # domain at all), stock's ``_ai_tool_search(domain='')`` runs
    # ``json.loads('')`` → ValueError("Invalid JSON format for custom
    # domain") and the whole tool call dies on what should be an
    # unfiltered search. Inject the JSON-encoded empty domain for the
    # tools whose stock implementation json.loads-es these params.
    if fn_name in ("ir_actions_server_search",
                   "ir_actions_server_read_group"):
        for _dom_key in ("domain", "having"):
            if _dom_key == "having" and fn_name == "ir_actions_server_search":
                continue
            if not kwargs.get(_dom_key):
                kwargs[_dom_key] = "[]"
                _logger.info(
                    "daadit_ai_mistral.tool_dispatch: injected default "
                    "%s='[]' for %s on %s (LLM omitted the argument)",
                    _dom_key, fn_name, requested_model or "<unknown>",
                )

    # ------------------------------------------------------------------
    # Domain post-processing: non-searchable-field repair + hard
    # per-agent read scope (v19.0.4.8.0 — see helper section above).
    # ------------------------------------------------------------------
    _domain_notes = []
    if fn_name in _DOMAIN_TOOLS and requested_model and env is not None:
        _dom_raw = kwargs.get("domain")
        _dom = None
        try:
            _dom = json.loads(_dom_raw) if isinstance(_dom_raw, str) \
                else _dom_raw
        except (TypeError, ValueError):
            _dom = None
        if isinstance(_dom, list):
            # (1) Translate/strip conditions the ORM cannot search.
            try:
                _dom, _domain_notes = _sanitize_unsearchable_domain(
                    env, requested_model, _dom,
                )
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "daadit_ai_mistral.tool_dispatch: domain sanitize "
                    "failed for %s on %s — using original domain",
                    fn_name, requested_model,
                )
            # (2) Hard read scope — fail-closed: a scope line whose
            # domain can't be applied blocks the read rather than
            # silently widening it.
            if hasattr(agent, "_daadit_read_scope_domain"):
                try:
                    _scope = agent._daadit_read_scope_domain(
                        requested_model
                    )
                except Exception as exc:  # noqa: BLE001
                    _logger.exception(
                        "daadit_ai_mistral.tool_dispatch: read-scope "
                        "lookup failed for agent=%s model=%s — "
                        "failing closed", agent.id, requested_model,
                    )
                    return {"error": (
                        f"This agent's read scope for "
                        f"'{requested_model}' could not be applied "
                        f"({exc}). The read was blocked; ask an "
                        f"administrator to fix the agent's read-scope "
                        f"configuration."
                    )}
                if _scope:
                    _dom = _domain_and([_scope, _dom])
                    _logger.info(
                        "daadit_ai_mistral.tool_dispatch: read scope "
                        "enforced for agent=%s on %s (scope=%s)",
                        agent.id, requested_model, _scope,
                    )
            kwargs["domain"] = json.dumps(_dom, default=str)

    # ------------------------------------------------------------------
    # read_group aggregate normalisation (v19.0.4.1.2)
    # ------------------------------------------------------------------
    # Mistral spells the record-count aggregate as `count` / `id` /
    # `count(*)` — Odoo's ORM only accepts the literal `__count`, and
    # a bare field name without `:method` is likewise rejected. Both
    # failure modes burned tool-loop iterations on prod (logs #88/#89).
    # Normalise the well-known count spellings; leave everything else
    # (e.g. `amount_total:sum`) untouched.
    if fn_name == "ir_actions_server_read_group":
        aggs = kwargs.get("aggregates")
        if isinstance(aggs, str):
            aggs = [aggs]
        if isinstance(aggs, list):
            fixed = []
            changed = False
            for a in aggs:
                key = str(a).strip().lower() if a is not None else ""
                if key in _COUNT_AGGREGATE_SPELLINGS:
                    fixed.append("__count")
                    changed = True
                else:
                    fixed.append(a)
            # Dedupe while preserving order (count + id → one __count).
            seen_aggs = set()
            deduped = []
            for a in fixed:
                if a not in seen_aggs:
                    seen_aggs.add(a)
                    deduped.append(a)
            kwargs["aggregates"] = deduped
            if changed:
                _logger.info(
                    "daadit_ai_mistral.tool_dispatch: normalised "
                    "aggregates %s → %s for %s on %s",
                    aggs, deduped, fn_name, requested_model or "<unknown>",
                )
        elif not aggs:
            # No aggregates at all → counting records is the only
            # sensible default for a grouped query.
            kwargs["aggregates"] = ["__count"]
            _logger.info(
                "daadit_ai_mistral.tool_dispatch: injected default "
                "aggregates=['__count'] for %s on %s",
                fn_name, requested_model or "<unknown>",
            )

    # ------------------------------------------------------------------
    # Unexpected-kwarg strip-retry (v19.0.4.1.5)
    # ------------------------------------------------------------------
    # Prod log #115: the model passed ``fields=[...]`` to read_group,
    # which doesn't take it — a whole tool-loop iteration burned on a
    # kwarg that could simply be dropped. Strip kwargs the method
    # rejects (max 3, defensive) and retry instead of failing. Any
    # TypeError that is NOT an unexpected-kwarg (or survives the
    # strips) falls through to the existing handler below.
    def _strip_invalid_field(bad_field):
        """Remove ``bad_field`` from fields/groupby/aggregates in kwargs.

        Handles the ``field:operator`` spelling on groupby/aggregates
        (e.g. ``x_bad:sum`` → matches ``x_bad``). Returns True if
        anything was removed, so the caller knows a retry is worthwhile.
        """
        removed = False
        vals = kwargs.get("fields")
        if isinstance(vals, list) and bad_field in vals:
            kwargs["fields"] = [f for f in vals if f != bad_field]
            removed = True
        for key in ("groupby", "aggregates"):
            vals = kwargs.get(key)
            if isinstance(vals, list):
                kept = [
                    v for v in vals
                    if (str(v).split(":", 1)[0].strip() != bad_field)
                ]
                if len(kept) != len(vals):
                    kwargs[key] = kept
                    removed = True
        return removed

    def _dispatch_with_kwarg_strip():
        for _attempt in range(6):
            try:
                if action is not None:
                    # Preferred path: stock's executor validates the
                    # action's ai_tool_schema and runs the action BODY —
                    # including any operator guard code — exactly like
                    # the native AI flow.
                    return action._ai_tool_run(agent, kwargs)
                return method(**kwargs)
            except TypeError as exc:
                m_unexpected = re.search(
                    r"unexpected keyword argument '([^']+)'", str(exc),
                )
                if (
                    _attempt < 5
                    and m_unexpected
                    and m_unexpected.group(1) in kwargs
                ):
                    bad_kwarg = m_unexpected.group(1)
                    kwargs.pop(bad_kwarg)
                    _logger.info(
                        "daadit_ai_mistral.tool_dispatch: %s rejected "
                        "kwarg %r — dropped it and retrying",
                        fn_name, bad_kwarg,
                    )
                    continue
                raise
            except ValueError as exc:
                # mistral-medium routinely hallucinates extra field names
                # alongside valid ones (prod: product.template searched
                # with x_product_owner_id PLUS invented x_product_manager_id
                # etc.). Odoo rejects the WHOLE call on the first invalid
                # field, so the valid field never gets read and the user
                # gets no answer. Strip the offending field from
                # fields/groupby/aggregates and retry, keeping the valid
                # ones. (v19.0.4.2.6)
                m_field = re.search(r"Invalid field '([^']+)'", str(exc))
                if (
                    _attempt < 5
                    and m_field
                    and _strip_invalid_field(m_field.group(1))
                ):
                    _logger.info(
                        "daadit_ai_mistral.tool_dispatch: %s rejected "
                        "invalid field %r — dropped it and retrying "
                        "(remaining fields=%s)",
                        fn_name, m_field.group(1), kwargs.get("fields"),
                    )
                    continue
                raise

    try:
        result = _dispatch_with_kwarg_strip()
    except TypeError as exc:
        sig_hint = _build_signature_hint(method, fn_name)
        _logger.warning(
            "daadit_ai_mistral.tool_dispatch: %s(**%r) raised TypeError: %s",
            method_name, kwargs, exc,
        )
        if env is not None:
            _record_in_ir_logging(
                env, "WARNING", "daadit_ai_mistral.tool_dispatch",
                f"TYPEERROR fn={fn_name} args={json.dumps(kwargs, default=str)[:1500]} "
                f"err={exc} | {sig_hint}",
            )
        return {"error": (
            f"Tool {fn_name} signature error: {exc}. {sig_hint} "
            f"Send required args as a JSON object."
        )}
    except Exception as exc:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.tool_dispatch: %s raised", method_name,
        )
        if env is not None:
            _record_in_ir_logging(
                env, "ERROR", "daadit_ai_mistral.tool_dispatch",
                f"RAISED fn={fn_name} args={json.dumps(kwargs, default=str)[:1500]} "
                f"err_type={type(exc).__name__} err={exc}",
            )
        return {"error": (
            f"Tool {fn_name} raised {type(exc).__name__}: {exc}. "
            f"Check the argument types — domain must be a JSON array of "
            f"triples, e.g. [[\"state\", \"=\", \"posted\"]]."
        )}

    # ------------------------------------------------------------------
    # Field-level privacy gate (response side).
    # Scrub blocked keys from the tool result before the LLM ever sees
    # them. No-op when daadit_field_blocklist is empty or when the
    # result shape isn't dict / list-of-dicts.
    # ------------------------------------------------------------------
    if _model_for_priv and result is not None:
        try:
            result = agent._daadit_scrub_result(_model_for_priv, result)
        except Exception:  # noqa: BLE001
            # Defensive: a scrub failure must never break the call —
            # log and forward the unscrubbed result.
            _logger.exception(
                "daadit_ai_mistral.tool_dispatch: scrub_result failed for "
                "%s on model %s", method_name, _model_for_priv,
            )

    safe = _safe_jsonable(result)
    # ------------------------------------------------------------------
    # Binary-field strip (v19.0.3.13.2 — fix B of three).
    # Drops image_*, avatar_*, datas, raw, db_datas, *_binary keys
    # recursively. Base64 binary content has no use in chat context
    # and routinely 50-500KB per record. Done BEFORE size measurement
    # so a model that explicitly asked for image fields still gets
    # the rest of the record back instead of a hard error.
    # ------------------------------------------------------------------
    safe = _strip_binary_fields(safe)
    # ------------------------------------------------------------------
    # Hard size cap (v19.0.3.13.2 — fix A of three).
    # If the JSON-serialised result still exceeds the cap after the
    # binary strip, swap it out for an actionable error dict so the
    # LLM can re-call with a tighter scope. Without this, an oversized
    # tool result lands in the next request and trips Mistral's
    # "prompt too large" HTTP 400.
    # ------------------------------------------------------------------
    safe = _enforce_result_size_cap(safe, fn_name)
    # Surface domain rewrites (non-searchable-field repair) to the
    # model so it knows the result set may be wider than it asked for.
    if _domain_notes and isinstance(safe, dict):
        safe.setdefault("daadit_domain_notes", _domain_notes)
    _logger.info(
        "daadit_ai_mistral.tool_dispatch: %s ok (args_keys=%s, "
        "result_type=%s)",
        method_name, list(kwargs.keys()), type(result).__name__,
    )
    if env is not None:
        # Cap result size in the log row so we don't fill up the table.
        try:
            result_summary = json.dumps(safe, default=str)[:1500]
        except Exception:  # noqa: BLE001
            result_summary = str(safe)[:1500]
        _record_in_ir_logging(
            env, "INFO", "daadit_ai_mistral.tool_dispatch",
            f"OK fn={fn_name} args={json.dumps(kwargs, default=str)[:1500]} "
            f"result={result_summary}",
        )
    return safe
