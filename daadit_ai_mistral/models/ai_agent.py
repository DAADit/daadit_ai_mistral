# -*- coding: utf-8 -*-
"""Extends ``ai.agent`` with Mistral models and routes calls to Mistral.

================================================================================
SELECTION EXTENSION — works regardless of Enterprise source visibility
================================================================================
The stock ``ai.agent.llm_model`` is declared with a callable selection
(``_get_llm_model_selection``). We override that method and append Mistral
entries to the base list. This is the *only* correct way to add options to a
callable selection — ``selection_add=[...]`` raises an AssertionError on
non-list selections (Odoo 19 enforces this in fields_selection.py).

================================================================================
DISPATCH OVERRIDE — needs verification against the installed Enterprise source
================================================================================
We override candidate dispatch methods using Odoo's standard ``_inherit`` +
``super()`` pattern. Odoo's registry will merge the methods via MRO regardless
of whether the parent class actually defines them — but if the parent does NOT
have a method, our ``super()`` call will raise ``AttributeError`` for non-Mistral
models. To stay safe we wrap the super-call in a guard.

The candidate names tried are:

    * ``_get_llm_response``   (most likely in 19.0)
    * ``_call_llm``           (older convention)
    * ``_make_llm_request``   (alt convention)

Once you've confirmed which one Odoo's stock module actually uses (grep on
your Odoo.sh dev branch in ``odoo/addons/ai/models/ai_agent.py``), simplify
this file to keep only the override that matches.
================================================================================
"""
import logging

from markupsafe import Markup

from odoo import _, api, fields, models

from ..services.mistral_client import (
    is_mistral_model,
    is_mistral_embedding_model,
)
# SEC H4 (v19.0.3.11.0): MistralClient and SUPPORTED_MODELS are no
# longer imported here. The legacy ``_daadit_call_mistral`` path that
# used them has been removed; all Mistral traffic now flows through
# ``services.llm_api_patch`` which imports MistralClient itself.
from ..services import registry_patches
from ..services import llm_api_patch
from ..services import tool_dispatch
from ..services import diagnostics

_logger = logging.getLogger(__name__)

# Per-chunk cap for _ai_tool_search_knowledge. Five chunks of raw
# article text can otherwise dwarf the rest of the run's context.
_KNOWLEDGE_CHUNK_CHARS = 2000


# Mistral model labels for the selection field. Order = display order.
MISTRAL_MODEL_SELECTION = [
    ("mistral-large-latest", "Mistral Large (latest)"),
    ("mistral-medium-latest", "Mistral Medium (latest)"),
    ("mistral-small-latest", "Mistral Small (latest)"),
    ("codestral-latest", "Codestral (latest)"),
    ("pixtral-large-latest", "Pixtral Large — vision (latest)"),
    ("ministral-8b-latest", "Ministral 8B (latest)"),
    ("ministral-3b-latest", "Ministral 3B (latest)"),
]


class AIAgent(models.Model):
    _inherit = "ai.agent"

    # ------------------------------------------------------------------ #
    # Selection extension                                                #
    #                                                                    #
    # The stock ``ai`` module declares ``llm_model`` with a callable     #
    # selection that is captured **by reference** at field-declaration   #
    # time (``selection=_get_llm_model_selection``). A by-reference      #
    # callable does NOT walk the registry MRO at runtime, so simply      #
    # overriding the method on this _inherit class would have no effect. #
    #                                                                    #
    # We therefore re-declare the field with ``selection="<name>"`` as   #
    # a *string* — that forces Odoo to resolve the method by name via    #
    # the merged registry class on every call, which means our override  #
    # below actually runs and ``super()`` correctly delegates to the     #
    # parent's implementation to fetch the OpenAI + Gemini base list.    #
    # ------------------------------------------------------------------ #
    # ``ondelete`` deliberately does NOT use ``"set default"`` — stock
    # ``llm_model`` is required-without-default (same shape that forced the
    # explicit fallback on ai.embedding.embedding_model). Falling back to
    # ``gpt-4o`` is safe because OpenAI is always present in the parent
    # selection — even when this module is uninstalled.
    llm_model = fields.Selection(
        selection="_get_llm_model_selection",
        ondelete={key: "set gpt-4o" for key, _label in MISTRAL_MODEL_SELECTION},
    )

    # ------------------------------------------------------------------ #
    # Per-agent Mistral overrides                                        #
    # ------------------------------------------------------------------ #
    # SEC L4 (v19.0.3.11.0): the temperature override now uses an
    # explicit boolean flag (``daadit_mistral_temperature_active``) as
    # the gate. This way a value of 0.0 (legitimate "deterministic"
    # mode) is honoured when the flag is on, and the field ignored
    # when off — instead of treating 0.0 as "fallback" which made
    # deterministic mode unreachable.
    daadit_mistral_temperature_active = fields.Boolean(
        string="Apply temperature override",
        default=False,
        help=(
            "When unticked, the agent's response_style controls the "
            "temperature (analytical=0.2, balanced=0.6, creative=0.9). "
            "Tick this to force the value below — including 0.0 for "
            "fully deterministic responses, which the response_style "
            "mapping cannot produce on its own."
        ),
    )
    daadit_mistral_temperature = fields.Float(
        string="Mistral temperature override",
        digits=(3, 2),
        default=0.0,
        help=(
            "Override the temperature sent to Mistral for THIS agent. "
            "Effective only when 'Apply temperature override' is ticked. "
            "Range 0.0 – 1.5; values >1 produce more random output."
        ),
    )
    daadit_mistral_max_tokens = fields.Integer(
        string="Mistral max tokens override",
        default=0,
        help=(
            "Cap the completion length for THIS agent. 0 = no cap "
            "(Mistral default). Useful for cost control on chatty agents."
        ),
    )

    # ------------------------------------------------------------------ #
    # Orchestrator mode (Robin)                                          #
    #                                                                    #
    # When set, the chat loop keeps ONLY Ask Agent + Open Agent Chat.    #
    # The agent may discuss everything with the user, but never executes #
    # domain tools itself — it asks specialists or opens a chat so the   #
    # user can continue with them.                                       #
    # ------------------------------------------------------------------ #
    daadit_is_orchestrator = fields.Boolean(
        string="Orchestrator (no own tools)",
        default=False,
        help=(
            "Tick for the concierge (Robin). The agent keeps only "
            "'AI: Ask Agent' and 'AI: Open Agent Chat': it asks "
            "specialists and returns their answers, or opens a new "
            "chat with a specialist so the user can continue there. "
            "All other tools (search, write, …) are stripped even if "
            "they are still linked via topics."
        ),
    )

    # ------------------------------------------------------------------ #
    # Per-agent model access control                                     #
    #                                                                    #
    # Tools that take a ``model_name`` parameter (Search, Read group,    #
    # Get Fields, Open Menu *) are gated against these two lists before  #
    # being dispatched to the stock ``_ai_tool_*`` methods. The check    #
    # runs in ``tool_dispatch.run_tool_call`` BEFORE Odoo's RBAC fires,  #
    # so even if the calling user technically has read access on a      #
    # model, the agent itself can be scoped to a smaller set.            #
    # ------------------------------------------------------------------ #
    daadit_allowed_model_ids = fields.Many2many(
        "ir.model",
        relation="daadit_ai_mistral_agent_allowed_model_rel",
        column1="agent_id",
        column2="model_id",
        string="Allowed models",
        help=(
            "If set, this agent can ONLY query the listed models. "
            "Empty = unrestricted (the agent can query any model the "
            "calling user has access to). Use this to scope a "
            "sales-only agent to ['sale.order', 'res.partner', "
            "'product.template'], etc."
        ),
    )
    daadit_blocked_model_ids = fields.Many2many(
        "ir.model",
        relation="daadit_ai_mistral_agent_blocked_model_rel",
        column1="agent_id",
        column2="model_id",
        string="Blocked models",
        help=(
            "Models the agent must NEVER query, regardless of the "
            "allowed list. Useful for explicitly denying access to "
            "sensitive models like 'res.users', 'ir.config_parameter', "
            "'mail.message', 'ir.attachment'. Blocked models always win "
            "over allowed models."
        ),
    )
    daadit_field_blocklist = fields.Char(
        string="Forbidden fields (PII)",
        help=(
            "Comma-separated list of `model.field` combinations that "
            "must never leave this agent (data-minimisation gate of "
            "last resort, working on top of the model whitelist). "
            "Matching keys are scrubbed from search_read / read tool "
            "responses, and filter conditions referencing a blocked "
            "field are rejected so the LLM can't binary-search the "
            "value through the search domain. Example: "
            "'res.partner.vat,hr.employee.identification_id,"
            "res.partner.bank_ids'. Matches the same syntax as "
            "`mcp.instance.field_blocklist` for consistency across "
            "DAADit modules."
        ),
    )

    # SEC L3: validate the syntax of the field-blocklist at save time
    # so a typo doesn't silently degrade the privacy gate.
    @api.constrains("daadit_field_blocklist")
    def _check_daadit_field_blocklist(self):
        import re
        from odoo.exceptions import ValidationError
        # ``model.field`` shape — Odoo model names are dot-separated
        # lowercase identifiers, field names are valid Python idents.
        entry_re = re.compile(
            r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+\.[a-z_][a-z0-9_]*$"
        )
        for rec in self:
            raw = rec.daadit_field_blocklist or ""
            if not raw.strip():
                continue
            for entry in raw.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                if not entry_re.match(entry):
                    raise ValidationError(
                        "Field blocklist entry %r is not in the "
                        "required 'model.field' shape (e.g. "
                        "'res.partner.vat'). Use lowercase, dot-"
                        "separated identifiers; one entry per "
                        "comma-separated chunk." % entry
                    )
                # Best-effort: warn (via logger) if the model exists
                # but the field doesn't — keeps obvious typos from
                # silently failing-open. We don't raise on a missing
                # field because the agent might be configured against
                # multiple databases with different installed apps.
                model_name, _, field_name = entry.rpartition(".")
                model = self.env["ir.model"].sudo().search(
                    [("model", "=", model_name)], limit=1,
                )
                if not model:
                    _logger.warning(
                        "daadit_ai_mistral: blocklist entry %r references "
                        "model '%s' which is not installed in this DB — "
                        "the entry will be inert until the app is "
                        "installed.", entry, model_name,
                    )
                    continue
                field = self.env["ir.model.fields"].sudo().search([
                    ("model", "=", model_name),
                    ("name", "=", field_name),
                ], limit=1)
                if not field:
                    _logger.warning(
                        "daadit_ai_mistral: blocklist entry %r references "
                        "field '%s' which does not exist on model '%s' — "
                        "is this a typo? The entry will be inert.",
                        entry, field_name, model_name,
                    )

    def _daadit_is_model_allowed(self, model_name):
        """Return True iff this agent is permitted to query ``model_name``.

        Logic:
          * Empty model_name → False (defensive).
          * Model is in the blocked list → False (always wins).
          * No allowed list configured → True (unrestricted).
          * Model is in the allowed list → True.
          * Otherwise → False.

        Cheap to call: at most two Many2many reads, both already loaded
        as part of the agent record.
        """
        self.ensure_one()
        if not model_name:
            return False
        # Reads on ir.model.model (a Char field) — bulk fetch.
        if self.daadit_blocked_model_ids:
            blocked = set(self.daadit_blocked_model_ids.mapped("model"))
            if model_name in blocked:
                return False
        if not self.daadit_allowed_model_ids:
            return True
        allowed = set(self.daadit_allowed_model_ids.mapped("model"))
        return model_name in allowed

    # ------------------------------------------------------------------
    # Field-level blocklist — PII gate of last resort. Same semantics as
    # mcp.instance.field_blocklist so admins see one consistent rule
    # set across both modules.
    # ------------------------------------------------------------------
    def _daadit_blocked_field_set(self, model_name):
        """Return the set of field names blocked for ``model_name``."""
        self.ensure_one()
        if not self.daadit_field_blocklist or not model_name:
            return set()
        prefix = model_name + "."
        out = set()
        for entry in self.daadit_field_blocklist.split(","):
            entry = entry.strip()
            if entry.startswith(prefix):
                out.add(entry[len(prefix):])
        return out

    def _daadit_scrub_record(self, model_name, record):
        """Wipe blocked field keys from a record dict, in place.

        Returns the record so the caller can use it in a comprehension.
        Non-dict input is returned untouched (defensive)."""
        self.ensure_one()
        if not isinstance(record, dict):
            return record
        for f in self._daadit_blocked_field_set(model_name):
            if f in record:
                record[f] = None
        return record

    def _daadit_scrub_result(self, model_name, result):
        """Apply the field blocklist to a tool result.

        Handles the two shapes the search/read family of tools returns:
        a list of dicts (search_read) or a single dict (read by id).
        Other shapes (counts, ids, etc.) are returned unchanged.
        """
        self.ensure_one()
        if not self._daadit_blocked_field_set(model_name):
            return result
        if isinstance(result, list):
            return [self._daadit_scrub_record(model_name, r) for r in result]
        if isinstance(result, dict):
            # Some tools return {"records": [...], ...} — scrub if present.
            if isinstance(result.get("records"), list):
                result["records"] = [
                    self._daadit_scrub_record(model_name, r)
                    for r in result["records"]
                ]
                return result
            return self._daadit_scrub_record(model_name, result)
        return result

    def _daadit_domain_uses_blocked_field(self, model_name, domain):
        """Return the offending field if ``domain`` filters on a blocked
        field on ``model_name``, else ''."""
        self.ensure_one()
        blocked = self._daadit_blocked_field_set(model_name)
        if not blocked or not domain:
            return ""
        # ``domain`` may already be a list (parsed by the tool) or still
        # a JSON string (depending on caller). Be forgiving.
        if isinstance(domain, str):
            try:
                import json as _json
                domain = _json.loads(domain)
            except Exception:  # noqa: BLE001
                return ""
        for clause in domain or []:
            if isinstance(clause, (list, tuple)) and len(clause) == 3:
                field = clause[0] or ""
                if field in blocked:
                    return field
        return ""

    # ------------------------------------------------------------------ #
    # Presets — one-click sane starting points so admins who don't know  #
    # the technical model names of Odoo can still configure access      #
    # control. Only models that actually exist in this database are     #
    # added; missing ones are silently skipped.                          #
    # ------------------------------------------------------------------ #

    # Curated list of business-facing models common to most Odoo
    # installations. Bias: read-heavy customer/sales/inventory data.
    # Excludes res.users / ir.* / mail.* (those are in the block list).
    _DAADIT_SUGGESTED_ALLOWED = [
        # CRM + Contacts
        "res.partner", "res.partner.category",
        "crm.lead", "crm.team", "crm.stage", "crm.tag",
        "calendar.event",
        # Sales
        "sale.order", "sale.order.line", "sale.report",
        # Purchases
        "purchase.order", "purchase.order.line",
        # Products + Inventory
        "product.template", "product.product", "product.category",
        "stock.picking", "stock.move", "stock.move.line",
        "stock.quant", "stock.location", "stock.warehouse",
        # Accounting (read-only-friendly)
        "account.move", "account.move.line", "account.account",
        "account.journal", "account.payment",
        # HR
        "hr.employee", "hr.department", "hr.job",
        "hr.leave", "hr.leave.type", "hr.attendance",
        # Projects / tasks
        "project.project", "project.task", "project.tags",
        # Manufacturing
        "mrp.production", "mrp.bom", "mrp.workorder",
        # Helpdesk (Enterprise — only if installed)
        "helpdesk.ticket", "helpdesk.team", "helpdesk.stage",
        # Subscription / Field Service / Recruitment (Enterprise)
        "sale.subscription",
        "industry.fsm.task",
        "hr.applicant", "hr.recruitment.stage",
        # Generic
        "res.company", "res.country", "res.currency",
    ]

    # Sensitive models that should never be exposed to AI tool calls
    # regardless of the user's access rights — credentials, attachments,
    # private messages, system internals.
    _DAADIT_SUGGESTED_BLOCKED = [
        # Identity / credentials
        "res.users", "res.users.log", "res.users.apikeys",
        "res.users.identitycheck",
        "auth.session.expired",
        # Configuration / system parameters
        "ir.config_parameter", "ir.cron", "ir.cron.trigger",
        # Attachments / private messages
        "ir.attachment", "mail.message", "mail.tracking.value",
        "mail.notification",
        # Logging / audit
        "ir.logging",
        # Model + permission metadata
        "ir.model", "ir.model.access", "ir.model.fields",
        "ir.model.data", "ir.model.constraint",
        "ir.rule", "ir.actions.server",
        # Mistral-internal: never expose own usage rows via AI
        "daadit_ai_mistral.usage",
    ]

    def action_daadit_apply_suggested_allowed_models(self):
        """Pre-fill ``daadit_allowed_model_ids`` with a curated list of
        common business models that exist in this database. Replaces
        the existing list (so the action is idempotent — clicking
        twice doesn't append duplicates)."""
        self.ensure_one()
        Model = self.env["ir.model"].sudo()
        existing = Model.search([("model", "in", self._DAADIT_SUGGESTED_ALLOWED)])
        self.daadit_allowed_model_ids = [(6, 0, existing.ids)]
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": "Allowed models applied",
                "message": (
                    f"{len(existing)} of {len(self._DAADIT_SUGGESTED_ALLOWED)} "
                    f"suggested business models exist in this database "
                    f"and are now on the allowed list. Adjust as needed."
                ),
                "sticky": False,
            },
        }

    def action_daadit_apply_default_blocked_models(self):
        """Pre-fill ``daadit_blocked_model_ids`` with sensitive system
        models. Same idempotent semantics as the allowed action."""
        self.ensure_one()
        Model = self.env["ir.model"].sudo()
        existing = Model.search([("model", "in", self._DAADIT_SUGGESTED_BLOCKED)])
        self.daadit_blocked_model_ids = [(6, 0, existing.ids)]
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": "Block list applied",
                "message": (
                    f"{len(existing)} of {len(self._DAADIT_SUGGESTED_BLOCKED)} "
                    f"sensitive system models exist in this database "
                    f"and are now on the block list."
                ),
                "sticky": False,
            },
        }

    def action_daadit_clear_allowed_models(self):
        """Reset the allowed list to empty (= unrestricted)."""
        self.ensure_one()
        self.daadit_allowed_model_ids = [(5, 0, 0)]
        return True

    def action_daadit_clear_blocked_models(self):
        """Reset the blocked list to empty."""
        self.ensure_one()
        self.daadit_blocked_model_ids = [(5, 0, 0)]
        return True

    # ------------------------------------------------------------------ #
    # write() override — diagnostic logging                              #
    #                                                                    #
    # Odoo logs reveal the "No embedding model found" UserError is       #
    # raised during ``web_save``, not during ``open_agent_chat``. So the #
    # offending method is invoked from ``write()`` — likely via an       #
    # @api.constrains or a compute_method on a related field.           #
    # We log the full traceback so we can identify which stock method   #
    # in the call stack actually performs the provider → embedding      #
    # lookup. Remove this override once the chain is known.              #
    # ------------------------------------------------------------------ #
    def write(self, vals):
        is_mistral_change = (
            "llm_model" in vals
            and is_mistral_model(vals.get("llm_model"))
        )
        if is_mistral_change:
            _logger.info(
                "daadit_ai_mistral: write() llm_model='%s' on agents=%s",
                vals.get("llm_model"),
                self.ids,
            )
            try:
                return super().write(vals)
            except Exception as exc:  # noqa: BLE001
                import traceback
                _logger.error(
                    "daadit_ai_mistral: write() raised %s: %s\n"
                    "FULL TRACEBACK:\n%s",
                    type(exc).__name__,
                    exc,
                    traceback.format_exc(),
                )
                raise
        return super().write(vals)

    @api.model
    def _get_llm_model_selection(self):
        # Guard against the case where the parent's callable selection
        # function is named differently (e.g. ``_compute_llm_models``). If
        # no parent method exists we'd otherwise return Mistral-only and
        # break OpenAI/Gemini selection. The fallback names are best
        # guesses for stock; we still log a warning so the issue surfaces.
        parent = getattr(super(), "_get_llm_model_selection", None)
        base = []
        if callable(parent):
            try:
                base = list(parent() or [])
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "daadit_ai_mistral: super()._get_llm_model_selection raised; "
                    "continuing with empty base list"
                )
        else:
            for cand in ("_compute_llm_models", "_get_models",
                         "_selection_llm_model", "_default_llm_models"):
                fn = getattr(super(), cand, None)
                if callable(fn):
                    try:
                        base = list(fn() or [])
                        _logger.info(
                            "daadit_ai_mistral: parent selection method "
                            "resolved as '%s'", cand,
                        )
                        break
                    except Exception:  # noqa: BLE001
                        continue
            if not base:
                _logger.warning(
                    "daadit_ai_mistral: could not find a parent llm_model "
                    "selection method; OpenAI/Gemini entries may be missing."
                )
        existing_keys = {value for value, _label in base}
        # Dynamic source: the live-synced registry (daadit.ai.mistral.model),
        # refreshed daily from the Mistral /v1/models API and via the manual
        # "Refresh models" button. Newly-released Mistral models appear here
        # automatically — no redeploy needed. Fall back to the built-in seed
        # constant when the table is empty or unreachable (fresh
        # install/upgrade before seed data loads, or a DB error).
        entries = []
        try:
            entries = self.env["daadit.ai.mistral.model"].sudo()._selection_entries()
        except Exception:  # noqa: BLE001
            _logger.debug(
                "daadit_ai_mistral: model registry unavailable; using seed",
                exc_info=True,
            )
        if not entries:
            entries = list(MISTRAL_MODEL_SELECTION)
        added = 0
        for value, label in entries:
            if value not in existing_keys:
                base.append((value, label))
                existing_keys.add(value)
                added += 1
        _logger.debug(
            "daadit_ai_mistral: _get_llm_model_selection extended with %d "
            "Mistral entries (base size=%d, total=%d)",
            added,
            len(base) - added,
            len(base),
        )
        return base

    # ------------------------------------------------------------------ #
    # Registry hook — dynamic provider→embedding lookup patching         #
    #                                                                    #
    # The candidate dispatch overrides at the bottom of this file are    #
    # static guesses at the real method name on closed-source stock     #
    # ``ai.agent``. When none of them match, the form save raises:       #
    #     UserError("No embedding model found for the selected provider")#
    # because stock's lookup neither finds 'mistral' as a provider nor   #
    # is intercepted by us.                                               #
    #                                                                    #
    # ``_register_hook`` runs once per registry build, after every       #
    # ``_inherit`` has been merged. We use it to:                        #
    #                                                                    #
    #   1. Bytecode-scan every method on the merged class for the error  #
    #      string and, if found, install a wrapper that returns          #
    #      'mistral-embed' for Mistral agents (delegating otherwise).    #
    #   2. Find class-level dicts that look like provider→embedding-     #
    #      model maps and patch in 'mistral': 'mistral-embed' in place.  #
    #                                                                    #
    # See services/registry_patches.py for the implementation.           #
    # ------------------------------------------------------------------ #

    @api.model
    def _register_hook(self):
        res = super()._register_hook()
        try:
            self._daadit_install_provider_patches()
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral: provider patching on ai.agent failed"
            )
        # Read the diag flag from ir.config_parameter and conditionally
        # install the UserError trace tap. Off by default — see
        # services/diagnostics.py for the rationale.
        try:
            diagnostics.maybe_install_trace_tap_from_env(self.env)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral: trace-tap toggle raised; continuing"
            )
        return res

    @api.model
    def _daadit_install_provider_patches(self):
        cls = type(self)

        targets = registry_patches.discover_lookup_methods(cls)
        method_names = [name for name, _base in targets]
        if targets:
            _logger.info(
                "daadit_ai_mistral: ai.agent bytecode scan found "
                "embedding-lookup methods: %s",
                [(n, b.__module__) for n, b in targets],
            )
        else:
            _logger.info(
                "daadit_ai_mistral: ai.agent bytecode scan found no "
                "embedding-lookup methods (this is fine if the lookup "
                "is on ai.embedding instead)."
            )

        patched_methods = registry_patches.install_method_overrides(
            cls,
            method_names,
            is_target_record=lambda rec: bool(
                is_mistral_model(getattr(rec, "llm_model", None) or "")
            ),
            target_return_value="mistral-embed",
            log_label="daadit_ai_mistral[ai.agent]",
        )

        dict_specs = registry_patches.discover_provider_dicts(cls)
        patched_dicts = registry_patches.patch_provider_dicts(
            dict_specs,
            log_label="daadit_ai_mistral[ai.agent]",
        )

        # --- Global module scan ----------------------------------------
        # Stock provider→embedding lookup may live in a service/helper
        # module that's not part of the registered ai.agent class
        # hierarchy (and therefore invisible to ``discover_lookup_methods``
        # above). Walk every loaded module under ``odoo.addons.ai*`` and
        # wrap any callable whose bytecode references the error string.
        # We run this from ai.agent's hook (and not also from
        # ai.embedding's) because the global scan is process-wide — once
        # is enough.
        global_targets = registry_patches.discover_in_loaded_modules(
            module_prefix="odoo.addons.ai",
        )
        # Also scan ai_app explicitly in case its module name doesn't
        # nest under ``odoo.addons.ai`` (e.g. ``odoo.addons.ai_app``
        # technically does match, but other 3rd-party prefixes wouldn't).
        for extra_prefix in ("odoo.addons.ai_app", "odoo.addons.ai_crm",
                             "odoo.addons.ai_documents"):
            for spec in registry_patches.discover_in_loaded_modules(
                module_prefix=extra_prefix,
            ):
                if spec not in global_targets:
                    global_targets.append(spec)

        if global_targets:
            _logger.info(
                "daadit_ai_mistral: global module scan found %d "
                "embedding-lookup callables: %s",
                len(global_targets),
                [(getattr(p, "__name__", str(p)), n)
                 for p, n, _ in global_targets],
            )
        else:
            _logger.info(
                "daadit_ai_mistral: global module scan found no "
                "embedding-lookup callables in odoo.addons.ai*."
            )

        patched_globals = registry_patches.install_module_overrides(
            global_targets,
            target_return_value="mistral-embed",
            is_mistral_string_predicate=is_mistral_model,
            is_mistral_record_predicate=is_mistral_embedding_model,
            log_label="daadit_ai_mistral[global]",
        )

        _logger.info(
            "daadit_ai_mistral: ai.agent registry patches — "
            "shadowed methods=%s, patched dicts=%s, global wraps=%s",
            patched_methods, patched_dicts, patched_globals,
        )

        # --- LLMApiService chat-dispatch patch -------------------------
        # Stock ``ai.agent._generate_response`` constructs
        # ``odoo.addons.ai.utils.llm_api_service.LLMApiService(env=…,
        # provider=self._get_provider()).request_llm(…)``. With our
        # ``_get_provider()`` override returning 'mistral', the unpatched
        # LLMApiService raises ``NotImplementedError("Unsupported
        # provider: mistral")``. Patch the class so 'mistral' is accepted
        # and routes through MistralClient.
        try:
            llm_api_patch.patch_llm_api_service()
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral: LLMApiService patching failed"
            )

    # ------------------------------------------------------------------ #
    # Mistral dispatch                                                   #
    # ------------------------------------------------------------------ #
    #
    # SEC H4 (v19.0.3.11.0): the legacy ``_daadit_call_mistral`` direct-
    # client path and the ``_get_llm_response`` / ``_call_llm`` /
    # ``_make_llm_request`` candidate dispatch overrides have been
    # REMOVED. They predated the LLMApiService monkey-patch (see
    # services/llm_api_patch.py) and reached the Mistral API without
    # going through the per-agent allow/block-list, the field-level
    # blocklist, the domain-validation gate, or the threadlocal
    # cleanup. On Odoo 19 stock chat goes through LLMApiService, so
    # these overrides were unreachable — but if any third-party module
    # had ever called one of those candidate names directly, every
    # security gate this module advertises would silently be skipped.
    #
    # All Mistral chat traffic now flows through:
    #
    #   ai.agent._generate_response()
    #     → _get_provider() (override below — sets threadlocal)
    #     → LLMApiService(env, 'mistral').request_llm(...)
    #     → llm_api_patch._request_llm_mistral
    #         (gates: model allow/block, field blocklist, domain check)
    #         → tool_dispatch.run_tool_call (per-tool RBAC)
    #         → MistralClient.chat_completion
    #
    # which is the only audited path.
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Provider lookup overrides                                          #
    #                                                                    #
    # Stock ai.agent has a method that maps ``llm_model`` to a provider  #
    # name (likely ``'openai'`` / ``'google'``) and raises ``UserError(  #
    # "No provider found for the selected model")`` for unknown models. #
    # We override candidate names so Mistral models map to ``'mistral'`` #
    # and the lookup succeeds.                                           #
    # ------------------------------------------------------------------ #

    def _get_provider_for_model(self, *args, **kwargs):
        target = kwargs.get("model") or (args[0] if args else self.llm_model)
        if is_mistral_model(target):
            _logger.debug(
                "daadit_ai_mistral: _get_provider_for_model('%s') → 'mistral'",
                target,
            )
            return "mistral"
        try:
            return super()._get_provider_for_model(*args, **kwargs)
        except AttributeError:
            raise

    def _get_llm_provider(self, *args, **kwargs):
        if is_mistral_model(self.llm_model):
            _logger.debug(
                "daadit_ai_mistral: _get_llm_provider() → 'mistral'",
            )
            return "mistral"
        try:
            return super()._get_llm_provider(*args, **kwargs)
        except AttributeError:
            raise

    def _get_provider(self, *args, **kwargs):
        if is_mistral_model(self.llm_model):
            # Just-in-time self-heal — stock's _generate_response calls
            # _get_provider immediately before constructing LLMApiService,
            # so this is the last hook point we have before the class
            # __init__ runs. Idempotent: a no-op after the first call.
            try:
                llm_api_patch.patch_llm_api_service()
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "daadit_ai_mistral: just-in-time LLMApiService patch "
                    "raised during _get_provider"
                )
            # Stash this agent record on a threadlocal so the patched
            # request_llm can dispatch tool calls back to its
            # ``_ai_tool_*`` methods. Stock's call sequence is:
            #   ai.agent._generate_response()
            #     → self._get_provider()  ← (we're here, ``self`` is the agent)
            #     → LLMApiService(env, provider).request_llm(...)
            # so this is the only place we have ``self``-the-agent in
            # scope just before request_llm runs.
            try:
                tool_dispatch.current_agent.record = self
            except Exception:  # noqa: BLE001
                pass
            return "mistral"
        try:
            return super()._get_provider(*args, **kwargs)
        except AttributeError:
            raise

    # ------------------------------------------------------------------ #
    # Test button intercept                                              #
    #                                                                    #
    # The form-view's "Test" button calls ``open_agent_chat()``. If the  #
    # stock implementation eagerly validates the model's provider at    #
    # this stage and our provider-lookup overrides above haven't taken  #
    # effect, we still want a meaningful error rather than the cryptic  #
    # "No provider found".                                               #
    # ------------------------------------------------------------------ #

    def open_agent_chat(self, *args, **kwargs):
        if is_mistral_model(self.llm_model):
            _logger.info(
                "daadit_ai_mistral: open_agent_chat called for Mistral "
                "agent '%s' (model=%s)",
                self.name, self.llm_model,
            )
        try:
            return super().open_agent_chat(*args, **kwargs)
        except AttributeError:
            _logger.warning(
                "daadit_ai_mistral: ai.agent has no open_agent_chat — "
                "this should not happen, the form view declares the button."
            )
            raise

    # ------------------------------------------------------------------ #
    # Provider → embedding-model reverse lookup                          #
    #                                                                    #
    # After our provider-lookup returns ``'mistral'``, Odoo does the     #
    # inverse: "for provider X, which embedding model should I use?"     #
    # If no mapping exists it raises:                                    #
    #     UserError("No embedding model found for the selected provider")#
    # We override candidate methods so ``'mistral'`` → ``'mistral-embed'``#
    # ------------------------------------------------------------------ #

    def _get_embedding_model(self, *args, **kwargs):
        """The real hook — verified against the Enterprise source.

        ``ai.agent._get_embedding_model`` resolves the model by scanning
        ``llm_providers.PROVIDERS``, which has no Mistral entry, so it
        raises for a Mistral agent. The other overrides in this block
        are guesses at method names that do not exist in stock; this is
        the one Odoo actually calls (from ``_build_rag_context``).
        """
        if is_mistral_model(self.llm_model):
            return "mistral-embed"
        try:
            return super()._get_embedding_model(*args, **kwargs)
        except AttributeError:
            raise

    def _get_embedding_model_for_provider(self, *args, **kwargs):
        provider = kwargs.get("provider") or (args[0] if args else None)
        if provider == "mistral":
            _logger.debug(
                "daadit_ai_mistral: _get_embedding_model_for_provider"
                "('mistral') → 'mistral-embed'",
            )
            return "mistral-embed"
        try:
            return super()._get_embedding_model_for_provider(*args, **kwargs)
        except AttributeError:
            raise

    def _get_default_embedding_model(self, *args, **kwargs):
        provider = kwargs.get("provider") or (args[0] if args else None)
        if provider == "mistral" or is_mistral_model(self.llm_model):
            return "mistral-embed"
        try:
            return super()._get_default_embedding_model(*args, **kwargs)
        except AttributeError:
            raise

    def _get_provider_embedding_model(self, *args, **kwargs):
        provider = kwargs.get("provider") or (args[0] if args else None)
        if provider == "mistral":
            return "mistral-embed"
        try:
            return super()._get_provider_embedding_model(*args, **kwargs)
        except AttributeError:
            raise

    def _embedding_model_for_provider(self, *args, **kwargs):
        provider = kwargs.get("provider") or (args[0] if args else None)
        if provider == "mistral":
            return "mistral-embed"
        try:
            return super()._embedding_model_for_provider(*args, **kwargs)
        except AttributeError:
            raise

    # ------------------------------------------------------------------ #
    # DAADit AI write-tools (v19.0.4.0.0)                                #
    #                                                                    #
    # The stock ``ai`` module ships only read-only AI tools (Search,     #
    # Read group, Get Fields, Open Menu *, …). Without a write-side      #
    # tool an autonomous agent can REPORT but cannot ACT — the helpdesk  #
    # use case (assign tickets, schedule SLA-overdue activities) is then #
    # blocked at the dispatch layer regardless of prompt quality.        #
    #                                                                    #
    # These two methods are the underlying implementations behind the    #
    # ``AI: Assign User`` and ``AI: Schedule Activity`` server-actions   #
    # defined in ``data/ai_tools.xml``. Tool-name slugging follows the   #
    # standard pattern (``ir_actions_server_<slug>`` → ``_ai_tool_       #
    # <slug>``) so the existing ``tool_dispatch.run_tool_call`` routes   #
    # them with no special-casing.                                       #
    #                                                                    #
    # Security                                                           #
    # --------                                                           #
    # * Each method runs as ``self.env.user`` — i.e. the schedule's      #
    #   "Run as" user when invoked from a scheduled run. All standard    #
    #   Odoo ACLs and record rules therefore apply.                      #
    # * The dispatcher already gated ``model_name`` through              #
    #   ``_daadit_is_model_allowed`` before calling us; we don't repeat  #
    #   that check here (one place to maintain).                         #
    # * Failures return ``{"error": "<reason>"}`` JSON so the LLM can    #
    #   recover and try again — never raise into the chat loop.          #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Chatter attribution (v19.0.4.1.0)                                  #
    #                                                                    #
    # Every write-side AI tool posts an internal note on the target      #
    # record after it succeeds, attributed to this agent's own partner   #
    # — exactly like OdooBot's own messages appear under "OdooBot".      #
    #                                                                    #
    # Why: without attribution, a helpdesk manager who sees              #
    # "Assigned to Wytse" in a ticket's tracking has no way to tell      #
    # whether Nick did that manually or a scheduled agent did it. The    #
    # write-tools' create_uid is always the schedule's "Run as" user     #
    # (Nick for our current setup), so tracking would misattribute       #
    # every autonomous change to a human. The chatter note surfaces      #
    # the agent identity while `create_uid` on the mail.message row      #
    # keeps the real invoking user for audit.                            #
    #                                                                    #
    # The helper is a no-op on records that don't inherit                #
    # ``mail.thread``, and swallows any messaging error — a logging      #
    # hiccup must never break the tool call itself.                      #
    # ------------------------------------------------------------------ #

    def _daadit_post_agent_message(self, record, body):
        """Post an internal note on ``record`` attributed to this agent.

        Attribution: the chatter shows the agent's own partner as
        author (like OdooBot posts appear under "OdooBot"), while
        ``create_uid`` on the message row still records the actual
        invoking user for a full audit trail.

        No-op when the target record does not inherit ``mail.thread``.
        Never raises — a chatter/logging failure must not break the
        tool call.
        """
        self.ensure_one()
        if not record or not hasattr(record, "message_post"):
            return
        author_id = self.partner_id.id if self.partner_id else False
        try:
            record.message_post(
                body=body,
                author_id=author_id or False,
                message_type="notification",
                subtype_xmlid="mail.mt_note",
            )
        except Exception:  # noqa: BLE001
            _logger.warning(
                "daadit_ai_mistral: chatter attribution failed on "
                "%s(%s) by agent=%s — tool result unaffected",
                record._name, record.id, self.name,
            )

    def _daadit_post_channel_status(self, channel_id, body):
        """Post a short live progress line to a discuss channel in a
        separate, immediately-committed transaction. Best-effort:
        never raises — a status hiccup must not break the answer."""
        self.ensure_one()
        if not channel_id or not body:
            return
        author_id = self.partner_id.id if self.partner_id else False
        try:
            with self.env.registry.cursor() as new_cr:
                new_env = api.Environment(
                    new_cr, self.env.uid, dict(self.env.context),
                )
                channel = new_env["discuss.channel"].browse(channel_id).exists()
                if channel:
                    channel.message_post(
                        body=body,
                        author_id=author_id or False,
                        message_type="comment",
                        subtype_xmlid="mail.mt_comment",
                    )
                # new_cr commits on clean __exit__ → bus NOTIFY delivers it live
        except Exception:  # noqa: BLE001
            _logger.warning(
                "daadit_ai_mistral: interim status post failed on "
                "channel=%s by agent=%s — answer unaffected",
                channel_id, self.name,
            )

    def _daadit_delegation_status_body(self, target_name):
        """Dutch progress line shown while the concierge delegates a turn."""
        if target_name:
            return _("Even bij %s navragen…", target_name)
        return _("Even dit intern navragen…")

    def _ai_tool_assign_user(self, model_name=None, record_id=None,
                             user_id=None, **_extra):
        """Assign a user to a record by setting its ``user_id`` field.

        Parameters
        ----------
        model_name : str
            Technical model name of the target record (e.g.
            ``'helpdesk.ticket'``). Validated against the agent's
            allow/block lists by ``tool_dispatch`` before we run.
        record_id : int
            Database id of the record to assign.
        user_id : int
            Database id of the ``res.users`` to set as ``user_id`` on
            the record. Must be an active internal user.

        Returns
        -------
        dict
            ``{'ok': True, 'model_name': ..., 'record_id': ...,
            'user_id': ..., 'user_name': ...}`` on success;
            ``{'error': '<reason>'}`` on any validation failure.
        """
        self.ensure_one()
        if not model_name or not isinstance(model_name, str):
            return {"error": "model_name is required (technical model "
                             "name as a string, e.g. 'helpdesk.ticket')."}
        try:
            record_id = int(record_id) if record_id is not None else 0
            user_id = int(user_id) if user_id is not None else 0
        except (TypeError, ValueError):
            return {"error": "record_id and user_id must be integers."}
        if not record_id:
            return {"error": "record_id is required (integer)."}
        if not user_id:
            return {"error": "user_id is required (integer)."}
        if model_name not in self.env:
            return {"error": "Unknown model '%s'." % model_name}
        Model = self.env[model_name]
        field = Model._fields.get("user_id")
        if (
            field is None
            or getattr(field, "type", None) != "many2one"
            or getattr(field, "comodel_name", None) != "res.users"
        ):
            return {"error": (
                "Model '%s' has no 'user_id' many2one to res.users; "
                "cannot assign a user via this tool." % model_name
            )}
        user = self.env["res.users"].browse(user_id).exists()
        if not user:
            return {"error": "User id %s does not exist." % user_id}
        if not user.active:
            return {"error": (
                "User id %s ('%s') is archived; refusing to assign an "
                "inactive user." % (user_id, user.name)
            )}
        if user.share:
            return {"error": (
                "User id %s ('%s') is a portal/public user; only "
                "internal users can be assigned." % (user_id, user.name)
            )}
        record = Model.browse(record_id).exists()
        if not record:
            return {"error": (
                "Record id %s on '%s' does not exist." % (record_id, model_name)
            )}
        # SCOPE-GUARD vóór de write (taak 1079). Tot nu toe vroeg alleen
        # het plannen van een activiteit de schrijfscope op, dus kon deze
        # tool een gesloten ticket of een gevouwen fase toewijzen zolang
        # het record binnen de *lees*scope viel. De grens staat in de
        # scoperecords; hier wordt hij alleen opgevraagd.
        allowed, scope_reason = self._daadit_write_scope(
            model_name, record_id)
        if not allowed:
            _logger.warning(
                "SCOPE-GUARD blokkeerde assign_user: agent %s -> %s #%s",
                self.id, model_name, record_id)
            return {
                "ok": False,
                "written": False,
                "blocked_by_scope_guard": True,
                "error": scope_reason,
            }
        previous_user = record.user_id
        if previous_user.id == user.id:
            return {
                # 5-8-2026 (taak 779): een overgeslagen schrijfactie gaf
                # ok=True terug. Voor het model niet te onderscheiden van
                # succes — 75 keer in 7 dagen gerapporteerd als gedaan werk.
                # De boodschap eronder was steeds correct; de envelop niet.
                "ok": False,
                "written": False,
                "skipped": True,
                "reason": "already_assigned",
                "model_name": model_name,
                "record_id": record_id,
                "user_id": user.id,
                "user_name": user.name,
            }
        try:
            record.write({"user_id": user.id})
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "daadit_ai_mistral._ai_tool_assign_user: write failed "
                "on %s(%s) → user_id=%s as user=%s: %s",
                model_name, record_id, user.id, self.env.user.id, exc,
            )
            return {"error": (
                "Could not assign user: %s. Check that the calling "
                "user has write rights on '%s'." % (exc, model_name)
            )}
        self._daadit_post_agent_message(
            record,
            Markup(_(
                "🤖 <strong>%(agent)s</strong> assigned this to "
                "<strong>%(user)s</strong>."
            )) % {"agent": self.name, "user": user.name},
        )
        return {
            "ok": True,
            "model_name": model_name,
            "record_id": record_id,
            "user_id": user.id,
            "user_name": user.name,
            "previous_user_id": previous_user.id or None,
        }

    _THROTTLE_ICP_LIMIT = "daadit_ai_mistral.activity_throttle_per_day"
    _THROTTLE_ICP_FALLBACK = "daadit_ai_mistral.activity_throttle_fallback_user_id"
    _THROTTLE_DIGEST_SUMMARY = "Agent-activiteiten boven daglimiet (bundel)"
    _OPEN_PER_RECORD_ICP = "daadit_ai_mistral.open_activities_per_record"

    # Words that carry no topic: dropping them keeps the comparison on
    # the meaningful terms. NL + EN, since agents write in both.
    _ACTIVITY_STOPWORDS = frozenset("""
        de het een en of van voor naar op in bij te ten ter met dan als
        dat die deze dit is zijn wordt worden was waren er nog al niet
        geen om aan door over uit meer langer dan naar toe
        the a an and or of for to on in at by with is are was were be
        been this that these those not no more than into from
    """.split())

    # Het zelfherstel-pad. Argus dient een promptfix in als activiteit
    # met deze prefix; de applier pikt hem daar op. Waargenomen 01-08:
    # zijn eerste autonome fix strandde op de cap van twee open taken
    # per record, omdat Nick er al 42 open had staan op datzelfde
    # artikel. De cap is er om lijsten leesbaar te houden — niet om het
    # enige mechanisme te blokkeren waarmee het systeem zichzelf
    # repareert. Deze route komt hooguit een paar keer per maand langs.
    _SELF_HEAL_PREFIX = "AUTO-APPLY"

    @classmethod
    def _daadit_summary_starts_with_token(cls, summary, token):
        """True when ``summary`` starts with ``token`` after junk/emoji.

        Live AUTO-APPLY proposals often arrive as
        ``⚠️ AUTO-APPLY niet toepasbaar:…``. A bare ``startswith`` misses
        those and the open-activity cap then silently blocks the
        self-heal channel (taak 773).
        """
        import re as _re
        text = (summary or "").strip().upper()
        if not text:
            return False
        needle = (token or "").strip().upper()
        if not needle:
            return False
        if text.startswith(needle):
            return True
        cleaned = _re.sub(r"^[^0-9A-ZÀ-Ÿ]+", "", text)
        return cleaned.startswith(needle)

    @classmethod
    def _daadit_is_self_heal(cls, summary):
        return cls._daadit_summary_starts_with_token(
            summary, cls._SELF_HEAL_PREFIX,
        )

    @classmethod
    def _daadit_activity_tokens(cls, text):
        """Normalise a summary to a set of meaningful lowercase tokens."""
        import re as _re
        cleaned = _re.sub(r"[^0-9a-zà-ÿ]+", " ", (text or "").lower())
        return {
            token for token in cleaned.split()
            if len(token) > 1 and token not in cls._ACTIVITY_STOPWORDS
        }

    @classmethod
    def _daadit_is_rolling_signal(cls, summary):
        """Daily rolling lists (restlijst) must refresh, not dedup-skip.

        Jaccard topic-match collapses ``Restlijst assurance 2026-08-02:
        1 onopgeloste runfout`` with today's list of six runs, so the
        tool skipped creating/updating and the restlijst went dark for
        days (taak 774).
        """
        tokens = cls._daadit_activity_tokens(summary)
        return "restlijst" in tokens

    @classmethod
    def _daadit_same_activity_topic(cls, summary_a, summary_b):
        """True when two activity summaries describe the same to-do.

        Compared as normalised token sets with a Jaccard threshold, so
        rewordings of one signal collapse together
        ("Escalatie: Ticket langer dan 2 werkuur onopgelost" ≈ "Ticket
        >2 werkuur onopgelost — prioriteit herzien") while unrelated
        to-dos on the same record stay separate ("Ticket wacht op
        triage" vs "Geboekte uren zonder gekoppelde verkooporder").
        """
        if not summary_a or not summary_b:
            return False
        if summary_a.strip().lower() == summary_b.strip().lower():
            return True
        tokens_a = cls._daadit_activity_tokens(summary_a)
        tokens_b = cls._daadit_activity_tokens(summary_b)
        if not tokens_a or not tokens_b:
            return False
        union = tokens_a | tokens_b
        overlap = len(tokens_a & tokens_b) / len(union) if union else 0.0
        return overlap >= 0.5

    def _daadit_refresh_activity(self, activity, summary, note, deadline):
        """Update an open activity in place (rolling signals / cap path)."""
        self.ensure_one()
        vals = {
            "summary": (summary or "").strip() or activity.summary,
            "date_deadline": deadline or fields.Date.context_today(self),
        }
        if note:
            vals["note"] = note
        activity.write(vals)
        return {
            "ok": True,
            "written": True,
            "updated": True,
            "reason": "updated_existing",
            "message": (
                "Updated the existing open activity instead of creating "
                "a duplicate. Treat this as a successful write of the "
                "current signal."
            ),
            "activity_id": activity.id,
            "model_name": activity.res_model,
            "record_id": activity.res_id,
            "summary": activity.summary or "",
            "date_deadline": (
                activity.date_deadline.strftime("%Y-%m-%d")
                if activity.date_deadline else ""
            ),
            "user_id": activity.user_id.id,
            "user_name": activity.user_id.name,
        }

    def _daadit_activity_throttle(self, assignee, model_name, record,
                                  summary):
        """Fase-0 throttle: cap agent-created activities per user/day.

        Returns ``None`` when the activity may be created normally, or
        a result dict when the cap was hit and the item was bundled
        into the fallback reviewer's digest instead.

        Config (ir.config_parameter):
          * ``daadit_ai_mistral.activity_throttle_per_day`` — cap per
            assignee per day. Default 5; ``0`` disables the throttle.
          * ``daadit_ai_mistral.activity_throttle_fallback_user_id`` —
            res.users id that receives the bundled overflow digest.
            Unset → throttle logs a warning and lets the activity
            through (fail-open: a missing fallback must not silently
            swallow agent output).
        """
        self.ensure_one()
        icp = self.env["ir.config_parameter"].sudo()
        try:
            limit = int(icp.get_param(self._THROTTLE_ICP_LIMIT, "5") or 5)
        except (TypeError, ValueError):
            limit = 5
        if limit <= 0:
            return None

        today_start = fields.Datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        Activity = self.env["mail.activity"].sudo()
        count_today = Activity.search_count([
            ("daadit_agent_created", "=", True),
            ("user_id", "=", assignee.id),
            ("create_date", ">=", today_start),
        ])
        if count_today < limit:
            return None

        fallback = self.env["res.users"].browse()
        raw_fb = icp.get_param(self._THROTTLE_ICP_FALLBACK, "")
        try:
            if raw_fb:
                fallback = self.env["res.users"].sudo().browse(
                    int(raw_fb)
                ).exists()
        except (TypeError, ValueError):
            fallback = self.env["res.users"].browse()
        if not fallback or not fallback.active or fallback.share:
            _logger.warning(
                "daadit_ai_mistral activity throttle: cap (%d/day) hit "
                "for user %s but no valid fallback reviewer configured "
                "(%s) — letting the activity through. Set the ICP to a "
                "res.users id to activate bundling.",
                limit, assignee.id, self._THROTTLE_ICP_FALLBACK,
            )
            return None
        if fallback.id == assignee.id:
            # The overflow target IS the fallback reviewer — bundling
            # onto themselves adds nothing; let it through.
            return None

        # Bundle into one open digest activity on the fallback user's
        # partner record (res.partner carries the activity mixin).
        entry = (
            "<li>%s #%s — %s (voor: %s)</li>" % (
                model_name, record.id,
                summary or "(geen samenvatting)",
                assignee.display_name,
            )
        )
        digest = Activity.search([
            ("daadit_agent_created", "=", True),
            ("user_id", "=", fallback.id),
            ("res_model", "=", "res.partner"),
            ("res_id", "=", fallback.partner_id.id),
            ("summary", "=", self._THROTTLE_DIGEST_SUMMARY),
        ], limit=1)
        if digest:
            digest.write({"note": (digest.note or "") + entry})
        else:
            act_type = self.env.ref(
                "mail.mail_activity_data_todo", raise_if_not_found=False,
            )
            digest = Activity.create({
                "activity_type_id": act_type.id if act_type else False,
                "res_model": "res.partner",
                "res_model_id": self.env["ir.model"]._get_id("res.partner"),
                "res_id": fallback.partner_id.id,
                "summary": self._THROTTLE_DIGEST_SUMMARY,
                "note": (
                    "<p>Agent-activiteiten die de daglimiet (%d per "
                    "gebruiker) overschreden — beoordeel en verdeel "
                    "handmatig:</p><ul>%s</ul>" % (limit, entry)
                ),
                "date_deadline": fields.Date.context_today(self),
                "user_id": fallback.id,
                "daadit_agent_created": True,
            })
        _logger.info(
            "daadit_ai_mistral activity throttle: cap (%d/day) hit for "
            "user %s — bundled into digest activity %s for %s",
            limit, assignee.id, digest.id, fallback.login,
        )
        return {
            # Throttle bundled the item elsewhere — this assignee did
            # not get a new activity. Keep the envelope honest (779).
            "ok": False,
            "written": False,
            "throttled": True,
            "skipped": True,
            "reason": "daily_throttle",
            "message": (
                "Daily agent-activity cap (%d) reached for user %s. "
                "The item was added to the overflow digest for %s "
                "instead of creating a new activity. Report it as "
                "bundled, not as created." % (
                    limit, assignee.display_name, fallback.display_name,
                )
            ),
            "digest_activity_id": digest.id,
            "model_name": model_name,
            "record_id": record.id,
        }

    def _ai_tool_search_knowledge(self, query=None, top_n=5, **_extra):
        """Semantic search over this agent's own knowledge sources.

        Stock only retrieves in ``ai.agent._build_rag_context``, which
        runs on the interactive chat path. The scheduled runner in
        ``daadit_ai_agent_schedule`` calls ``request_llm`` directly, so a
        scheduled agent can never reach its own sources. Pre-flight
        retrieval would not fix that either: a schedule's prompt is a
        task instruction ("run your hourly scan"), not the question that
        needs answering — that only surfaces mid-run, inside a tool
        result. So retrieval has to be a tool the agent calls with the
        question once it has found it.

        Returns ``{'ok': True, 'chunks': [{source, url, content}, ...]}``.
        An empty ``chunks`` list means the sources hold no answer — the
        caller is expected to say so rather than invent one.
        """
        self.ensure_one()
        query = (query or "").strip()
        if not query:
            return {"error": "query is required — pass the question verbatim."}
        if not self.sources_ids:
            return {"ok": True, "chunks": [],
                    "reason": "this agent has no knowledge sources attached"}
        try:
            top_n = max(1, min(int(top_n or 5), 10))
        except (TypeError, ValueError):
            top_n = 5

        try:
            from odoo.addons.ai.utils.llm_api_service import LLMApiService
        except ImportError:
            return {"error": "Odoo AI LLMApiService is not importable"}

        # v19.0.4.10.0 made get_embedding Mistral-aware; without that
        # patch this call raises "Unsupported provider 'mistral'".
        llm_api_patch.patch_llm_api_service()
        embedding_model = self._get_embedding_model()
        try:
            response = LLMApiService(
                env=self.env, provider=self._get_provider(),
            ).get_embedding(
                input=query,
                dimensions=self.env["ai.embedding"]._get_dimensions(),
                model=embedding_model,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral: knowledge search failed to embed query"
            )
            return {"error": "could not embed the query: %s" % exc}

        data = (response or {}).get("data") or []
        if not data:
            return {"error": "the embedding service returned no vector"}

        chunks = self.env["ai.embedding"]._get_similar_chunks(
            query_embedding=data[0]["embedding"],
            sources=self.sources_ids,
            embedding_model=embedding_model,
            top_n=top_n,
        )
        if not chunks:
            return {"ok": True, "chunks": [],
                    "reason": "no matching passage in this agent's sources"}

        source_by_checksum = {
            s.attachment_id.checksum: s
            for s in self.sources_ids.filtered(lambda s: s.attachment_id)
        }
        results = []
        for chunk in chunks:
            source = source_by_checksum.get(chunk.attachment_id.checksum)
            results.append({
                "source": source.name if source else "",
                "url": (source.url or "") if source else "",
                "content": (chunk.content or "")[:_KNOWLEDGE_CHUNK_CHARS],
            })
        _logger.info(
            "daadit_ai_mistral: knowledge search agent=%s q=%d chars -> %d chunk(s)",
            self.id, len(query), len(results),
        )
        return {"ok": True, "chunks": results}

    def _ai_tool_schedule_activity(self, model_name=None, record_id=None,
                                   activity_type_xmlid=None,
                                   activity_type_id=None,
                                   summary=None, note=None,
                                   date_deadline=None, user_id=None,
                                   **_extra):
        """Create a ``mail.activity`` on a record.

        Idempotent: if an OPEN activity with the same activity type,
        summary and assignee already exists on the record, no new
        activity is created. Lets the helpdesk agent re-run safely
        within the same SLA window without piling up duplicates.

        Parameters
        ----------
        model_name : str
            Technical model name of the record (e.g.
            ``'helpdesk.ticket'``). Model must inherit
            ``mail.activity.mixin``.
        record_id : int
            Database id of the record.
        activity_type_xmlid : str, optional
            XML id of the ``mail.activity.type`` to use, e.g.
            ``'mail.mail_activity_data_todo'``. One of
            ``activity_type_xmlid`` or ``activity_type_id`` should be
            given; if both are missing we fall back to
            ``mail.mail_activity_data_todo`` (the standard "To Do"
            type).
        activity_type_id : int, optional
            Database id of the ``mail.activity.type``.
        summary : str, optional
            Short title for the activity.
        note : str, optional
            HTML body for the activity (defaults to empty).
        date_deadline : str, optional
            ISO date ``'YYYY-MM-DD'`` (or ``'YYYY-MM-DD HH:MM:SS'``,
            time part is ignored). Defaults to today.
        user_id : int, optional
            Database id of the user the activity is assigned to.
            Defaults to ``record.user_id`` if it exists, otherwise the
            calling user.

        Returns
        -------
        dict
            ``{'ok': True, 'written': True, 'activity_id': <id>, ...}``
            on create or refresh; ``{'ok': False, 'written': False,
            'skipped': True, 'reason': 'duplicate'|'open_activity_cap',
            'existing_activity_id': <id>, ...}`` when nothing was
            written; ``{'error': '<reason>'}`` on validation failure.
        """
        self.ensure_one()
        if not model_name or not isinstance(model_name, str):
            return {"error": "model_name is required (technical model "
                             "name as a string)."}
        try:
            record_id = int(record_id) if record_id is not None else 0
        except (TypeError, ValueError):
            return {"error": "record_id must be an integer."}
        if not record_id:
            return {"error": "record_id is required (integer)."}
        if model_name not in self.env:
            return {"error": "Unknown model '%s'." % model_name}
        Model = self.env[model_name]
        if not hasattr(Model, "activity_schedule"):
            # Naming the limitation alone made Sem repeat the same call
            # every day for three days (runs 496, 508, 536): his work
            # list is about website.page records, and an activity on the
            # record was the only way he knew to write a finding down.
            # Name where it CAN go instead.
            return {"error": self._daadit_no_activity_hint(model_name)}
        record = Model.browse(record_id).exists()
        if not record:
            return {"error": (
                "Record id %s on '%s' does not exist." % (record_id, model_name)
            )}

        # --- Resolve activity type (xmlid > id > fallback to "To Do") --
        ActivityType = self.env["mail.activity.type"].sudo()
        act_type = ActivityType.browse()
        if activity_type_id:
            try:
                act_type = ActivityType.browse(int(activity_type_id)).exists()
            except (TypeError, ValueError):
                act_type = ActivityType.browse()
        if not act_type and activity_type_xmlid:
            try:
                act_type = self.env.ref(activity_type_xmlid, raise_if_not_found=False)
                if act_type and act_type._name != "mail.activity.type":
                    act_type = ActivityType.browse()
            except Exception:  # noqa: BLE001
                act_type = ActivityType.browse()
        if not act_type:
            act_type = self.env.ref(
                "mail.mail_activity_data_todo", raise_if_not_found=False,
            )
        if not act_type:
            return {"error": (
                "Could not resolve a mail.activity.type. Pass a valid "
                "activity_type_xmlid or activity_type_id, or ensure the "
                "'mail.mail_activity_data_todo' fallback exists."
            )}

        # --- Resolve assignee -----------------------------------------
        assignee = None
        if user_id:
            try:
                assignee = self.env["res.users"].browse(int(user_id)).exists()
            except (TypeError, ValueError):
                return {"error": "user_id must be an integer."}
            if not assignee:
                return {"error": "User id %s does not exist." % user_id}
            if not assignee.active or assignee.share:
                return {"error": (
                    "User id %s is archived or a portal user; cannot "
                    "assign an activity to them." % user_id
                )}
        if assignee is None:
            # Default: existing user_id on the record, else the caller.
            record_user = getattr(record, "user_id", None)
            if record_user and record_user.id:
                assignee = record_user
            else:
                assignee = self.env.user

        # --- Parse deadline -------------------------------------------
        deadline = None
        if date_deadline:
            from datetime import datetime as _dt
            raw = str(date_deadline).strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    deadline = _dt.strptime(raw, fmt).date()
                    break
                except ValueError:
                    continue
            if deadline is None:
                return {"error": (
                    "date_deadline %r is not a valid date. Use "
                    "'YYYY-MM-DD'." % date_deadline
                )}
        if deadline is None:
            deadline = fields.Date.context_today(self)

        target_summary = (summary or "").strip()
        target_note = note or ""

        # --- Idempotence: skip if an equivalent open activity exists ---
        # Matching on the EXACT summary text does not hold: the model
        # rewords the same signal every run ("Escalatie: Ticket langer
        # dan 2 werkuur onopgelost" → "Ticket >2 werkuur onopgelost —
        # prioriteit herzien" → …), so the check never fired and one
        # helpdesk ticket collected five identical open escalations for
        # the same assignee. Compare on MEANING instead (normalised
        # token overlap), so a rephrasing is recognised while a
        # genuinely different to-do on the same record still gets
        # through.
        existing_open = self.env["mail.activity"].search([
            ("res_model", "=", model_name),
            ("res_id", "=", record.id),
            ("activity_type_id", "=", act_type.id),
            ("user_id", "=", assignee.id),
        ])
        existing = self.env["mail.activity"]
        if existing_open:
            if not target_summary:
                # No summary to compare — any open activity of the same
                # type for this assignee is the same reminder.
                existing = existing_open[:1]
            else:
                for candidate in existing_open:
                    if self._daadit_same_activity_topic(
                        target_summary, candidate.summary or "",
                    ):
                        existing = candidate
                        break
        if existing:
            # Rolling daily signals (restlijst): refresh the open
            # activity with today's summary/note/deadline instead of
            # dropping the new content as a false duplicate (taak 774).
            if self._daadit_is_rolling_signal(target_summary):
                return self._daadit_refresh_activity(
                    existing, target_summary, target_note, deadline,
                )
            return {
                # 5-8-2026 (taak 779): een overgeslagen schrijfactie gaf
                # ok=True terug. Voor het model niet te onderscheiden van
                # succes — 75 keer in 7 dagen gerapporteerd als gedaan werk.
                # De boodschap eronder was steeds correct; de envelop niet.
                "ok": False,
                "written": False,
                "skipped": True,
                "reason": "duplicate",
                "message": (
                    "An open activity for this record and assignee "
                    "already covers this ("
                    + (existing.summary or "no summary")
                    + "). Nothing was created — do NOT reword it and "
                    "try again; report it as already outstanding."
                ),
                "model_name": model_name,
                "record_id": record.id,
                # Bewust GEEN activity_id: dat is het id van een
                # activiteit die deze agent niet heeft aangemaakt.
                "existing_activity_id": existing.id,
                "existing_summary": existing.summary or "",
                "activity_type_id": act_type.id,
                "user_id": assignee.id,
            }

        # --- Cap on OPEN activities per record+assignee ----------------
        # Second net under the topic comparison above: wording can drift
        # far enough that two summaries no longer overlap, and the
        # assignee still ends up with a stack of open to-dos on a single
        # record (ticket 683 collected five). Past the cap we stop
        # adding — the outstanding to-do is the reminder.
        try:
            open_cap = int(self.env["ir.config_parameter"].sudo().get_param(
                self._OPEN_PER_RECORD_ICP, "2",
            ) or 2)
        except (TypeError, ValueError):
            open_cap = 2
        if (
            open_cap > 0
            and len(existing_open) >= open_cap
            and not self._daadit_is_self_heal(summary)
        ):
            _logger.info(
                "daadit_ai_mistral._ai_tool_schedule_activity: %s already "
                "has %s open activities for user %s on %s(%s) — capped",
                self.name, len(existing_open), assignee.id,
                model_name, record.id,
            )
            # Prefer refreshing the newest open activity over dropping
            # the signal entirely (taak 773: full channel = silent loss).
            newest = existing_open.sorted("id", reverse=True)[:1]
            if newest and (
                self._daadit_is_rolling_signal(target_summary)
                or target_note
            ):
                return self._daadit_refresh_activity(
                    newest, target_summary, target_note, deadline,
                )
            return {
                # 5-8-2026 (taak 779): een overgeslagen schrijfactie gaf
                # ok=True terug. Voor het model niet te onderscheiden van
                # succes — 75 keer in 7 dagen gerapporteerd als gedaan werk.
                # De boodschap eronder was steeds correct; de envelop niet.
                "ok": False,
                "written": False,
                "skipped": True,
                "reason": "open_activity_cap",
                "message": (
                    "%s already has %s open to-do(s) on this record: %s. "
                    "No new activity was created. Report the situation "
                    "instead of adding another reminder — the run must "
                    "surface this as blocked, not as done." % (
                        assignee.name, len(existing_open),
                        "; ".join(
                            a.summary or "(no summary)"
                            for a in existing_open[:3]
                        ),
                    )
                ),
                "model_name": model_name,
                "record_id": record.id,
                "open_activity_ids": existing_open.ids,
                "user_id": assignee.id,
            }

        # --- Fase-0 throttle: max N agent-activiteiten per gebruiker
        # per dag (Governance & guardrails knowledge id 173, kernregel
        # 4). Overschot wordt gebundeld in één digest-activiteit voor
        # de fallback-reviewer in plaats van de assignee te blijven
        # bestoken. Beschermt de reviewcapaciteit van een klein team.
        # Zelfherstel gaat ook langs de dagbundel heen: gebundeld in een
        # verzameltaak zou de applier de fix nooit vinden, en dan is het
        # mechanisme er wel maar werkt het niet.
        if not self._daadit_is_self_heal(summary):
            throttled = self._daadit_activity_throttle(
                assignee, model_name, record, target_summary,
            )
            if throttled is not None:
                return throttled

        # --- Create the activity ---------------------------------------
        try:
            res_model_id = self.env["ir.model"]._get_id(model_name)
            activity = self.env["mail.activity"].create({
                "activity_type_id": act_type.id,
                "res_model": model_name,
                "res_model_id": res_model_id,
                "res_id": record.id,
                "summary": target_summary,
                "note": target_note,
                "date_deadline": deadline,
                "user_id": assignee.id,
                "daadit_agent_created": True,
            })
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "daadit_ai_mistral._ai_tool_schedule_activity: create "
                "failed on %s(%s) as user=%s: %s",
                model_name, record_id, self.env.user.id, exc,
            )
            return {"error": (
                "Could not schedule activity: %s. Check that the calling "
                "user has rights to create activities on '%s'." % (exc, model_name)
            )}
        self._daadit_post_agent_message(
            record,
            Markup(_(
                "🤖 <strong>%(agent)s</strong> scheduled a to-do for "
                "<strong>%(user)s</strong> (due %(deadline)s): %(summary)s"
            )) % {
                "agent": self.name,
                "user": assignee.name,
                "deadline": deadline.strftime("%Y-%m-%d"),
                "summary": target_summary or act_type.name or _("(no summary)"),
            },
        )
        return {
            "ok": True,
            "written": True,
            "model_name": model_name,
            "record_id": record.id,
            "activity_id": activity.id,
            "activity_type_id": act_type.id,
            "summary": target_summary or act_type.name or "",
            "date_deadline": deadline.strftime("%Y-%m-%d"),
            "user_id": assignee.id,
            "user_name": assignee.name,
        }

    def _ai_tool_assurance_coverage(self, **_extra):
        """Return verified schedule rows for Argus' dekkingscheck (772).

        Argus used to invent schedule links from agent ids
        (``web#id=<agent_id>&model=daadit.ai.agent.schedule``). This tool
        looks schedules up — including inactive ones — and returns only
        rows that exist, with ``id``, ``name``, ``active``, ``agent_id``.
        """
        self.ensure_one()
        if "daadit.ai.agent.schedule" not in self.env:
            return {
                "ok": False,
                "error": (
                    "Module daadit_ai_agent_schedule is not installed; "
                    "cannot list schedules."
                ),
            }
        Schedule = self.env["daadit.ai.agent.schedule"].with_context(
            active_test=False,
        ).sudo()
        rows = []
        for schedule in Schedule.search([], order="id"):
            rows.append({
                "id": schedule.id,
                "name": schedule.name or "",
                "active": bool(schedule.active),
                "agent_id": schedule.agent_id.id or False,
                "agent_name": schedule.agent_id.name or "",
            })
        return {
            "ok": True,
            "count": len(rows),
            "schedules": rows,
            "instruction": (
                "Every coverage finding MUST use a schedule id from this "
                "list. Never derive a schedule id from an agent id. "
                "Include id, name, active and agent_id in the finding."
            ),
        }

    # ------------------------------------------------------------------ #
    # Router tool (v19.0.4.2.0) — "AI: Ask Agent"                        #
    #                                                                    #
    # Lets a concierge agent delegate a question to a specialist agent   #
    # and return its answer as a tool result. Guardrails, all           #
    # non-negotiable (from the 2026-07-03 adversarial plan review):      #
    #                                                                    #
    # * MAX DEPTH 1 — a routed agent cannot route further. Enforced      #
    #   via ``tool_dispatch.router_state.depth`` AND by stripping the    #
    #   router tool from the sub-run's tool list, in BOTH the explicit   #
    #   build here and the topic-reconstruction path (defence in depth). #
    # * WIDTH BUDGET — at most 3 sub-runs per top-level turn             #
    #   (``router_state.calls``), so one turn can't stack dozens of      #
    #   sequential Mistral calls and hit the worker timeout.             #
    # * NO WRITES — write-side tools (Assign User, Schedule Activity)    #
    #   are stripped from every sub-run; routing reads, never mutates.   #
    # * Tight budget — sub-runs get MAX_ITER=4 in the Mistral loop      #
    #   (see llm_api_patch) instead of 6; exhaustion is reported as an   #
    #   error so the concierge falls back rather than relaying truncated #
    #   narration.                                                       #
    # * Threadlocal swap with guaranteed restore — during the sub-run    #
    #   the TARGET agent is active (its allowed/blocked models, PII     #
    #   blocklist and tools apply); the previous agent is restored in    #
    #   a finally block.                                                 #
    # * Same user, no sudo dispatch — the sub-run executes with the      #
    #   calling user's RBAC; sudo() is only used to read agent config    #
    #   fields (system_prompt, topics), mirroring the schedule module.   #
    # ------------------------------------------------------------------ #

    # Per-turn cap on how many sub-runs one concierge turn may launch.
    # Depth is bounded at 1; this bounds WIDTH so a multi-route turn (or
    # a model that re-routes a failing question every iteration) can't
    # stack dozens of sequential Mistral calls into one HTTP request and
    # trip the Odoo worker's limit_time_real. (v19.0.4.2.1)
    _DAADIT_ROUTER_MAX_CALLS_PER_TURN = 3

    # Argument-name aliases Mistral tends to pick instead of the exact
    # schema names. Mirrors the pattern in tool_dispatch._PARAM_ALIASES
    # (model→model_name etc.), applied here because ``ask_agent`` params
    # are swallowed by ``**_extra`` — no TypeError, so the generic
    # kwarg-repair path never fires for this tool. (v19.0.4.2.1)
    _DAADIT_ASK_AGENT_ALIASES = {
        "agent": "agent_name", "name": "agent_name",
        "target": "agent_name", "target_agent": "agent_name",
        "agent_id": "agent_name", "specialist": "agent_name",
        "query": "question", "prompt": "question",
        "message": "question", "text": "question", "vraag": "question",
    }

    @api.model
    def _daadit_seed_orchestrator(self):
        """Mark Robin / Ask AI as orchestrator and attach the handoff tool.

        Idempotent. Safe to call from migrations and post_init.
        """
        for name in ("Robin", "Ask AI"):
            agents = self.sudo().search([
                ("name", "=ilike", name),
                ("daadit_is_orchestrator", "=", False),
            ])
            if agents:
                agents.write({"daadit_is_orchestrator": True})
        ask = self.env.ref(
            "daadit_ai_mistral.ir_actions_server_ask_agent",
            raise_if_not_found=False,
        )
        open_chat = self.env.ref(
            "daadit_ai_mistral.ir_actions_server_open_agent_chat",
            raise_if_not_found=False,
        )
        if not ask or not open_chat or "ai.topic" not in self.env:
            return
        for topic in self.env["ai.topic"].sudo().search(
            [("tool_ids", "in", ask.ids)]
        ):
            if open_chat.id not in topic.tool_ids.ids:
                topic.write({"tool_ids": [(4, open_chat.id)]})

    def _daadit_orchestrator_mode(self):
        """True when this agent must only ask / hand off, never execute."""
        self.ensure_one()
        return bool(getattr(self, "daadit_is_orchestrator", False))

    def _daadit_routing_fallback_hint(self):
        """Recovery instruction after a failed route / handoff.

        Orchestrators must not fall back to domain tools — they ask
        another specialist or open a chat. Hybrid concierges keep the
        historical "use your own tools" degradation path.
        """
        self.ensure_one()
        if self._daadit_orchestrator_mode():
            return (
                "Ask another specialist via ir_actions_server_ask_agent, "
                "or open a chat with the specialist via "
                "ir_actions_server_open_agent_chat so the user can "
                "continue there. Do NOT search, write or execute domain "
                "tools yourself."
            )
        return "Answer with your own tools instead."

    def _daadit_resolve_named_agent(self, agent_name):
        """Resolve ``agent_name`` to an ``ai.agent`` or return an error dict.

        Shared by Ask Agent and Open Agent Chat so both tools accept the
        same aliases and the same unambiguous-name rules.
        """
        self.ensure_one()
        if not agent_name or not isinstance(agent_name, str):
            return None, {"error": (
                "Missing 'agent_name'. Re-call with parameters "
                "agent_name (exact specialist name) and the other "
                "required fields."
            )}
        Agent = self.env["ai.agent"]
        needle = agent_name.strip()
        safe = needle.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        target = Agent.search([("name", "=ilike", safe)], limit=1)
        if not target:
            matches = Agent.search([("name", "ilike", safe)], limit=2)
            if len(matches) > 1:
                return None, {"error": (
                    "Agent name '%s' is ambiguous (%s). Re-call with the "
                    "exact agent name." % (
                        needle, ", ".join(sorted(matches.mapped("name")))
                    )
                )}
            target = matches[:1]
        if not target:
            available = Agent.search([]).mapped("name")
            return None, {"error": (
                "No agent named '%s'. Available agents: %s. Re-call with "
                "one of these exact names. %s" % (
                    needle, ", ".join(sorted(available)),
                    self._daadit_routing_fallback_hint(),
                )
            )}
        if target.id == self.id:
            return None, {"error": (
                "Refusing to route to myself. %s"
                % self._daadit_routing_fallback_hint()
            )}
        return target, None

    def _ai_tool_ask_agent(self, agent_name=None, question=None, **_extra):
        """Delegate ``question`` to the agent named ``agent_name`` and
        return its final answer.

        Returns ``{'ok': True, 'agent': <name>, 'answer': <text>}`` on
        success, or ``{'error': '<reason>'}``. Error messages tell the
        concierge how to recover: a *recoverable* input error (missing
        param under an alias) instructs a re-call with the right names;
        other errors instruct an orchestrator-safe recovery (ask
        another specialist / open a chat) or, for hybrid concierges,
        a fallback to their own tools.
        """
        self.ensure_one()
        from ..services.llm_api_patch import _slug_tool_name

        # --- Alias repair: pull agent_name/question out of _extra -----
        if not agent_name or not question:
            for k, v in (_extra or {}).items():
                tgt = self._DAADIT_ASK_AGENT_ALIASES.get(k)
                if tgt == "agent_name" and not agent_name and isinstance(v, str):
                    agent_name = v
                elif tgt == "question" and not question and isinstance(v, str):
                    question = v

        if not agent_name or not isinstance(agent_name, str):
            # Recoverable: re-call with the right parameter name rather
            # than abandoning routing for the whole turn.
            return {"error": (
                "Missing 'agent_name'. Re-call ir_actions_server_ask_agent "
                "with parameters agent_name (e.g. 'Sales Agent') and "
                "question."
            )}
        if (
            not question
            or not isinstance(question, str)
            or not question.strip()
        ):
            return {"error": (
                "Missing 'question'. Re-call ir_actions_server_ask_agent "
                "with parameters agent_name and question (the full, "
                "self-contained question for the specialist)."
            )}

        depth = getattr(tool_dispatch.router_state, "depth", 0)
        if depth >= 1:
            return {"error": (
                "Routing depth limit reached: a routed agent cannot "
                "route further. %s" % self._daadit_routing_fallback_hint()
            )}

        # --- Per-turn width budget ------------------------------------
        calls = getattr(tool_dispatch.router_state, "calls", 0)
        if calls >= self._DAADIT_ROUTER_MAX_CALLS_PER_TURN:
            return {"error": (
                "Routing budget for this turn is used up. %s"
                % self._daadit_routing_fallback_hint()
            )}
        tool_dispatch.router_state.calls = calls + 1

        target, err = self._daadit_resolve_named_agent(agent_name)
        if err:
            return err
        # v19.0.6.5.4: route to non-Mistral agents too. The sub-run runs
        # on whichever provider the TARGET uses, so a Mistral concierge
        # can delegate to a Claude specialist (Sem, Vince, Maud, …)
        # instead of refusing. Falls back to the old refusal only when
        # no provider add-on claims the model.
        sub_provider = "mistral" if is_mistral_model(
            target.llm_model or ""
        ) else None
        sub_patch = None
        if sub_provider is None:
            try:
                from odoo.addons.daadit_ai_claude.services.claude_client import (
                    is_claude_model,
                )
                if is_claude_model(target.llm_model or ""):
                    from odoo.addons.daadit_ai_claude.services import (
                        llm_api_patch as sub_patch,
                    )
                    sub_provider = "anthropic"
            except ImportError:
                _logger.info(
                    "daadit_ai_mistral.router: daadit_ai_claude not "
                    "importable — cannot route to Claude agents"
                )
        if sub_provider is None:
            return {"error": (
                "Agent '%s' runs on model '%s', for which no provider "
                "path is available; cannot route. %s" % (
                    target.name, target.llm_model or "?",
                    self._daadit_routing_fallback_hint(),
                )
            )}

        # Delegating a question that was just refused on policy grounds
        # only pays off if the receiver may read what the caller may
        # not. Eva was denied on account.move.line and asked Bram in the
        # next call; Bram's whitelist does not hold it either, so the
        # hop cost two iterations and produced nothing. Runs 558, 501,
        # 500 and 499 all show it, with four different models.
        blocked_for_target = self._daadit_denied_for_target(target)
        if blocked_for_target:
            names = ", ".join(sorted(blocked_for_target))
            _logger.info(
                "daadit_ai_mistral.router: refusing hop %s(%s) -> %s(%s), "
                "receiver may not read %s either",
                self.name, self.id, target.name, target.id, names,
            )
            if self._daadit_orchestrator_mode():
                return {"error": (
                    "Agent '%s' is not permitted to read %s either, so "
                    "delegating this question cannot produce that data. "
                    "Do not invent figures. Tell the user it is NOT "
                    "ESTABLISHED and name the missing source, or open a "
                    "chat with a colleague who does have that access via "
                    "ir_actions_server_open_agent_chat."
                    % (target.name, names)
                )}
            return {"error": (
                "Agent '%s' is not permitted to read %s either, so "
                "delegating this question cannot produce that data. Do "
                "not ask another colleague for it. Report it as NOT "
                "ESTABLISHED, name the missing source, and continue with "
                "what you can establish yourself — never invent figures, "
                "names or amounts to fill the gap." % (target.name, names)
            )}
        try:
            llm_api_patch._notify_step(
                self,
                "Oké, ik vraag het even aan %s." % target.name,
                kind="route",
            )
        except Exception:  # noqa: BLE001
            pass

        # Build the sub-run tool list from the TARGET's topics. Strip
        # orchestrator tools (no chains / no handoffs) AND all
        # write-side tools: routing fetches an ANSWER, never a mutation
        # on the caller's behalf, so the draft-only policy holds across
        # the router boundary even when routing to a write-capable
        # agent (e.g. Helpdesk SLA).
        tool_names = []
        try:
            for action in target.sudo().topic_ids.tool_ids:
                if action.model_id and action.model_id.model == "ai.agent":
                    slug = _slug_tool_name(action.with_context(lang="en_US").name)
                    if (
                        slug
                        and slug not in tool_dispatch.ORCHESTRATOR_TOOL_SLUGS
                        and slug not in tool_dispatch.WRITE_SIDE_TOOL_SLUGS
                        and slug not in tool_names
                    ):
                        tool_names.append(slug)
        except Exception:  # noqa: BLE001
            tool_names = []

        messages = []
        sys_prompt = (target.sudo().system_prompt or "").strip()
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})
        # Pin the answer language to the calling user's language so a
        # question that Mistral happened to translate to English doesn't
        # come back English and mix into a Dutch concierge answer.
        lang_hint = self._daadit_language_hint()
        if lang_hint:
            messages.append({"role": "system", "content": lang_hint})
        messages.append({"role": "user", "content": question.strip()})

        try:
            from odoo.addons.ai.utils.llm_api_service import LLMApiService
            # Use the module-level llm_api_patch import. A local
            # ``from ..services import llm_api_patch`` here used to
            # shadow the name for the whole function and make the
            # earlier _notify_step call raise UnboundLocalError
            # (swallowed — so "Ik vraag het even aan …" never showed).
            llm_api_patch.patch_llm_api_service()
        except ImportError as exc:
            return {"error": (
                "LLM service unavailable (%s). %s"
                % (exc, self._daadit_routing_fallback_hint())
            )}

        prev_record = getattr(tool_dispatch.current_agent, "record", None)
        prev_exhausted = getattr(tool_dispatch.router_state, "exhausted", False)
        prev_sub_failed = getattr(tool_dispatch.router_state, "sub_failed", False)
        prev_denied = getattr(
            tool_dispatch.router_state, "sub_denied_model", "",
        )
        # Tool tallies are zeroed for the sub-run and restored after, so
        # what we report back is this delegate's own work and not the
        # caller's. See ``tool_dispatch.note_tool_call``.
        prev_calls_made = getattr(tool_dispatch.router_state, "calls_made", 0)
        prev_writes_made = getattr(tool_dispatch.router_state, "writes_made", 0)
        # Set state and run inside one try/finally so a raise anywhere —
        # including before request_llm — can never leak depth or the
        # active-agent record onto this worker thread.
        try:
            tool_dispatch.router_state.depth = depth + 1
            tool_dispatch.router_state.exhausted = False
            tool_dispatch.router_state.sub_denied_model = ""
            tool_dispatch.router_state.calls_made = 0
            tool_dispatch.router_state.writes_made = 0
            tool_dispatch.current_agent.record = target
            _logger.info(
                "daadit_ai_mistral.router: agent %s(%s) routing question "
                "to %s(%s) as user %s (depth %s->%s, call %s, %d tools)",
                self.name, self.id, target.name, target.id, self.env.uid,
                depth, depth + 1, calls + 1, len(tool_names),
            )
            if sub_patch is not None:
                # Cross-provider hop: patch the target's provider in
                # before we ask LLMApiService for it.
                sub_patch.patch_llm_api_service()
            service = LLMApiService(env=self.env, provider=sub_provider)
            result = service.request_llm(
                model=target.llm_model,
                inputs=messages,
                tools=tool_names,
            )
            exhausted = bool(
                getattr(tool_dispatch.router_state, "exhausted", False)
            )
            sub_failed = bool(
                getattr(tool_dispatch.router_state, "sub_failed", False)
            )
            denied_model = getattr(
                tool_dispatch.router_state, "sub_denied_model", "",
            ) or ""
            sub_calls = getattr(tool_dispatch.router_state, "calls_made", 0)
            sub_writes = getattr(tool_dispatch.router_state, "writes_made", 0)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "daadit_ai_mistral.router: sub-run on %s(%s) raised %s: %s",
                target.name, target.id, type(exc).__name__, exc,
            )
            return {"error": (
                "Routed agent '%s' failed (%s). %s" % (
                    target.name, type(exc).__name__,
                    self._daadit_routing_fallback_hint(),
                )
            )}
        finally:
            tool_dispatch.current_agent.record = prev_record
            tool_dispatch.router_state.depth = depth
            tool_dispatch.router_state.exhausted = prev_exhausted
            tool_dispatch.router_state.sub_failed = prev_sub_failed
            tool_dispatch.router_state.sub_denied_model = prev_denied
            tool_dispatch.router_state.calls_made = prev_calls_made
            tool_dispatch.router_state.writes_made = prev_writes_made

        if isinstance(result, (list, tuple)):
            answer = "\n\n".join(
                str(x) for x in result if x is not None
            ).strip()
        else:
            answer = str(result or "").strip()

        # The sub-run burned its whole iteration budget, or ended with a
        # (possibly translated) fallback sentinel, without producing a
        # real answer. Report failure so the concierge recovers instead
        # of relaying garbage. sub_failed is the language-independent
        # signal set inside the sentinel path; exhausted covers
        # MAX_ITER; the string check is a last-resort belt.
        if exhausted or sub_failed:
            _logger.info(
                "daadit_ai_mistral.router: sub-run on %s(%s) failed "
                "(exhausted=%s sub_failed=%s) — signalling recovery",
                target.name, target.id, exhausted, sub_failed,
            )
            # A policy denial has a nameable cause, and a delegating
            # manager must report it as unverified rather than fill the
            # gap with invented figures (run 463).
            if denied_model:
                return {"error": (
                    "Agent '%s' could not answer: reading model '%s' is "
                    "not permitted for that agent, so the data does not "
                    "exist for this run. Report this explicitly as NOT "
                    "ESTABLISHED and name the missing source. Do NOT "
                    "invent numbers, customer names, amounts or links to "
                    "fill the gap." % (target.name, denied_model)
                )}
            return {"error": (
                "Agent '%s' could not complete the question. %s Report "
                "anything you could not establish as NOT ESTABLISHED — "
                "never invent data." % (
                    target.name, self._daadit_routing_fallback_hint(),
                )
            )}
        if not answer or answer.lstrip().startswith((
            "_(Mistral wanted to call", "_(Empty response",
        )):
            return {"error": (
                "Agent '%s' returned no usable answer. %s"
                % (target.name, self._daadit_routing_fallback_hint())
            )}
        # What the delegate actually did, counted by the dispatcher —
        # not what it says it did. Robin relayed "Post is set as a draft
        # in the Social Marketing app" from Mark while the database held
        # no such post and Mark had called no tool; the caller had no way
        # to tell narration from fact. Now it does, and the instruction
        # is explicit enough that a concierge cannot pass off a claim of
        # created work as done.
        claim = {
            "ok": True,
            "agent": target.name,
            "answer": answer,
            "tool_calls_made": sub_calls,
            "write_actions_made": sub_writes,
        }
        if sub_writes == 0:
            claim["fact_check"] = (
                "%s made %s tool call(s) and NO write actions, so nothing "
                "was created, changed or scheduled. If the answer above "
                "claims otherwise, that claim is false: relay it as a "
                "PROPOSAL, never as completed work, and say plainly that "
                "it still has to be carried out." % (target.name, sub_calls)
            )
        else:
            claim["fact_check"] = (
                "%s made %s tool call(s), of which %s wrote to the "
                "database. Only describe as done what the answer above "
                "ties to a concrete record." % (
                    target.name, sub_calls, sub_writes,
                )
            )
        _logger.info(
            "daadit_ai_mistral.router: sub-run on %s(%s) made %s calls / "
            "%s writes", target.name, target.id, sub_calls, sub_writes,
        )
        return claim

    # ------------------------------------------------------------------ #
    # Handoff tool (v19.0.6.22.0) — "AI: Open Agent Chat"                #
    #                                                                    #
    # Opens (or reuses) a discuss channel of type ai_chat with the       #
    # named specialist and nudges the user's UI to that chat. Robin      #
    # never executes domain work himself; when the user wants to keep    #
    # talking with a specialist, this is the path.                       #
    # ------------------------------------------------------------------ #

    _DAADIT_OPEN_CHAT_ALIASES = {
        "agent": "agent_name", "name": "agent_name",
        "target": "agent_name", "target_agent": "agent_name",
        "agent_id": "agent_name", "specialist": "agent_name",
        "message": "opening_message", "question": "opening_message",
        "prompt": "opening_message", "text": "opening_message",
        "vraag": "opening_message", "context": "opening_message",
    }

    def _ai_tool_open_agent_chat(
        self, agent_name=None, opening_message=None, **_extra
    ):
        """Open a new user↔specialist chat and notify the UI.

        Returns ``{'ok': True, 'agent': <name>, 'channel_id': <id>, ...}``
        on success, or ``{'error': '<reason>'}``.
        """
        self.ensure_one()

        if not agent_name or not opening_message:
            for k, v in (_extra or {}).items():
                tgt = self._DAADIT_OPEN_CHAT_ALIASES.get(k)
                if tgt == "agent_name" and not agent_name and isinstance(v, str):
                    agent_name = v
                elif (
                    tgt == "opening_message"
                    and not opening_message
                    and isinstance(v, str)
                ):
                    opening_message = v

        depth = getattr(tool_dispatch.router_state, "depth", 0)
        if depth >= 1:
            return {"error": (
                "Handoff is only available from the orchestrator chat, "
                "not from inside a routed sub-run."
            )}

        target, err = self._daadit_resolve_named_agent(agent_name)
        if err:
            return err

        # Prefer stock open_agent_chat — it owns channel creation and
        # the client action that pops the chat window. We still locate
        # the channel afterwards so the bus payload has a concrete id,
        # and so we can post an optional opening message.
        action = None
        try:
            action = target.open_agent_chat()
        except Exception as exc:  # noqa: BLE001
            _logger.info(
                "daadit_ai_mistral.handoff: open_agent_chat on %s(%s) "
                "raised %s — falling back to channel lookup/create",
                target.name, target.id, type(exc).__name__,
            )
            action = None

        channel = self._daadit_channel_from_action(action)
        if not channel:
            channel = self._daadit_find_or_create_agent_chat(target)
        if not channel:
            return {"error": (
                "Could not open a chat with '%s'. %s"
                % (target.name, self._daadit_routing_fallback_hint())
            )}

        posted = False
        seed = (opening_message or "").strip() if opening_message else ""
        if seed:
            try:
                from markupsafe import escape as _esc
                # Preserve line breaks so a multi-line brief stays
                # readable in Discuss.
                html = "<p>%s</p>" % _esc(seed).replace("\n", "<br/>")
                channel.message_post(
                    body=Markup(html),
                    message_type="comment",
                    author_id=self.env.user.partner_id.id,
                    subtype_xmlid="mail.mt_comment",
                )
                posted = True
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "daadit_ai_mistral.handoff: could not post opening "
                    "message on channel %s", channel.id,
                )

        payload = {
            "channel_id": channel.id,
            "agent_id": target.id,
            "agent_name": target.name,
        }
        if isinstance(action, dict):
            # Only forward JSON-safe action keys the client can doAction.
            safe_action = {
                k: action[k]
                for k in (
                    "type", "tag", "name", "res_model", "res_id",
                    "views", "view_mode", "target", "context",
                    "params", "path",
                )
                if k in action
            }
            if safe_action.get("type"):
                payload["action"] = safe_action

        # Own cursor + immediate commit, same reason as denkstappen: a
        # whole chat turn is one transaction, and bus messages only
        # leave on commit. Without this the UI would open the specialist
        # chat only after Robin's confirmation is already posted.
        self._daadit_notify_open_agent_chat(payload)
        try:
            llm_api_patch._notify_step(
                self,
                "Ik open even de chat met %s." % target.name,
                kind="route",
            )
        except Exception:  # noqa: BLE001
            pass

        _logger.info(
            "daadit_ai_mistral.handoff: %s(%s) opened chat with %s(%s) "
            "as channel %s for user %s (posted=%s)",
            self.name, self.id, target.name, target.id, channel.id,
            self.env.uid, posted,
        )
        return {
            "ok": True,
            "opened": True,
            "agent": target.name,
            "channel_id": channel.id,
            "opening_message_posted": posted,
            "instruction": (
                "Chat with %s is open. Reply in ONE short sentence "
                "(e.g. 'Je kunt verder met %s.'). No recap, no more "
                "tools." % (target.name, target.name)
            ),
        }

    def _daadit_channel_from_action(self, action):
        """Pull a ``discuss.channel`` id out of a stock client action."""
        Channel = self.env["discuss.channel"]
        if not isinstance(action, dict):
            return Channel.browse()
        if (
            action.get("res_model") == "discuss.channel"
            and action.get("res_id")
        ):
            return Channel.browse(action["res_id"]).exists()
        for bag_name in ("context", "params"):
            bag = action.get(bag_name) or {}
            if not isinstance(bag, dict):
                continue
            for key in ("active_id", "default_active_id", "channel_id"):
                val = bag.get(key)
                if isinstance(val, int):
                    found = Channel.browse(val).exists()
                    if found:
                        return found
                if isinstance(val, str) and "discuss.channel_" in val:
                    try:
                        cid = int(val.rsplit("_", 1)[-1])
                    except ValueError:
                        continue
                    found = Channel.browse(cid).exists()
                    if found:
                        return found
        return Channel.browse()

    def _daadit_notify_open_agent_chat(self, payload):
        """Push the handoff bus event on a short-lived cursor (best-effort)."""
        partner = self.env.user.partner_id
        if not partner:
            return
        try:
            import odoo
            from odoo import api, SUPERUSER_ID
            partner_id = partner.id
            dbname = self.env.cr.dbname
            with odoo.registry(dbname).cursor() as cr2:
                env2 = api.Environment(cr2, SUPERUSER_ID, {})
                env2["bus.bus"]._sendone(
                    env2["res.partner"].browse(partner_id),
                    "daadit_open_agent_chat",
                    payload,
                )
                cr2.commit()
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral.handoff: bus notify failed for "
                "channel %s", payload.get("channel_id"),
            )

    def _daadit_find_or_create_agent_chat(self, target):
        """Return the user's newest ``ai_chat`` with ``target``, creating
        one when stock ``open_agent_chat`` did not leave one behind.

        Best-effort: channel schemas differ slightly across Odoo builds,
        so create failures are logged and return an empty recordset.
        """
        self.ensure_one()
        Channel = self.env["discuss.channel"]
        partner = self.env.user.partner_id
        domain = [
            ("channel_type", "=", "ai_chat"),
            ("ai_agent_id", "=", target.id),
        ]
        if partner:
            domain.append(
                ("channel_member_ids.partner_id", "in", [partner.id])
            )
        try:
            channel = Channel.search(
                domain, order="write_date desc, id desc", limit=1,
            )
        except Exception:  # noqa: BLE001
            channel = Channel.browse()
        if channel:
            return channel

        vals = {
            "name": target.name,
            "channel_type": "ai_chat",
            "ai_agent_id": target.id,
        }
        try:
            channel = Channel.create(vals)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral.handoff: could not create ai_chat "
                "for agent %s(%s)", target.name, target.id,
            )
            return Channel.browse()

        # Ensure the calling user is a member so the UI can open it.
        try:
            if partner and hasattr(channel, "add_members"):
                channel.add_members(partner_ids=partner.ids)
            elif partner and "channel_member_ids" in channel._fields:
                channel.write({
                    "channel_member_ids": [(0, 0, {
                        "partner_id": partner.id,
                    })],
                })
        except Exception:  # noqa: BLE001
            _logger.info(
                "daadit_ai_mistral.handoff: could not add user %s to "
                "channel %s (may already be a member)",
                self.env.uid, channel.id,
            )
        return channel

    def _daadit_no_activity_hint(self, model_name):
        """Message for a model that cannot carry an activity.

        Says where the finding CAN go, in this order: the record's own
        chatter when the model has one (the note lands on the record the
        agent is actually looking at), otherwise a project task. Without
        an alternative the agent has nowhere to put its work and simply
        retries tomorrow.
        """
        self.ensure_one()
        Model = self.env[model_name]
        if hasattr(Model, "message_post"):
            where = (
                "This model does have a chatter, so log your finding as "
                "a note on the record instead of as an activity."
            )
        else:
            where = (
                "This model has no chatter either, so the record itself "
                "cannot hold your finding."
            )
        return (
            "Model '%s' does not support activities (it does not inherit "
            "mail.activity.mixin), and it never will for this call — do "
            "not retry it on this model. %s If the finding needs an owner "
            "and a deadline, put it on a project.task (which does support "
            "activities) and reference the '%s' record id in the "
            "description." % (model_name, where, model_name)
        )

    def _daadit_denied_for_target(self, target):
        """Models refused to me this turn that ``target`` may not read
        either.

        Only the models this turn was actually denied on are considered
        — a policy refusal is the one hard signal that the caller wants
        data it cannot reach. The question text is not parsed: guessing
        which model a Dutch sentence needs would refuse legitimate
        delegations.
        """
        self.ensure_one()
        denied = getattr(tool_dispatch.router_state, "denied_models", None)
        if not denied:
            return set()
        checker = getattr(target, "_daadit_is_model_allowed", None)
        if not callable(checker):
            return set()
        out = set()
        for model_name in denied:
            try:
                if not checker(model_name):
                    out.add(model_name)
            except Exception:  # noqa: BLE001
                # Fail open: a bug in the check must never block a hop
                # that might have worked.
                _logger.exception(
                    "daadit_ai_mistral.router: allow-check raised for "
                    "target=%s model=%s", target.id, model_name,
                )
        return out

    def _daadit_language_hint(self):
        """Return a one-line system instruction pinning the sub-run's
        answer language to the calling user's language, or '' if
        unknown. Best-effort — a wrong guess only affects phrasing."""
        try:
            lang = (self.env.user.lang or "").split("_")[0].lower()
        except Exception:  # noqa: BLE001
            return ""
        names = {
            "nl": "Dutch", "en": "English", "fr": "French",
            "de": "German", "es": "Spanish", "it": "Italian",
        }
        label = names.get(lang)
        if not label:
            return ""
        return (
            "Answer in %s (the user's language), regardless of the "
            "language this question is phrased in." % label
        )

    # ------------------------------------------------------------------ #
    # Diagnostic introspection — temporary helper                        #
    #                                                                    #
    # When iterating against a closed-source Enterprise module without   #
    # SSH access, we need a way to discover the real method names from   #
    # the running registry. This method returns every callable on the   #
    # merged ai.agent class whose name contains 'provider', 'embedding', #
    # 'model', 'llm', 'chat', or 'completion' — together with the class #
    # in the MRO that defines it (so we can see whether it's stock or   #
    # one of our overrides) and its signature.                           #
    #                                                                    #
    # Call via MCP after deploy:                                          #
    #     odoo_execute_kw(model="ai.agent", method=                      #
    #         "_daadit_debug_introspect", args=[], kwargs={},            #
    #         allow_mutations=True)                                       #
    #                                                                    #
    # Remove this method once the dispatch chain is confirmed.           #
    # ------------------------------------------------------------------ #

    @api.model
    def _daadit_debug_introspect(self):
        import inspect
        keywords = ("provider", "embedding", "model", "llm",
                    "chat", "completion", "ai_", "agent",
                    "open_agent")
        cls = type(self)
        results = {}
        for name in sorted(dir(cls)):
            if name.startswith("__"):
                continue
            if not any(kw in name.lower() for kw in keywords):
                continue
            attr = getattr(cls, name, None)
            if not callable(attr):
                continue
            # Find which class in the MRO actually defined this method
            defining_cls = None
            for base in cls.__mro__:
                if name in base.__dict__:
                    defining_cls = base
                    break
            try:
                sig = str(inspect.signature(attr))
            except (ValueError, TypeError):
                sig = "(?)"
            results[name] = {
                "defined_in": (
                    f"{defining_cls.__module__}.{defining_cls.__name__}"
                    if defining_cls else "unknown"
                ),
                "signature": sig,
            }
        return results
