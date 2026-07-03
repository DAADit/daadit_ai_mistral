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
        added = 0
        for value, label in MISTRAL_MODEL_SELECTION:
            if value not in existing_keys:
                base.append((value, label))
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
        previous_user = record.user_id
        if previous_user.id == user.id:
            return {
                "ok": True,
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
            _(
                "🤖 <strong>%(agent)s</strong> assigned this record to "
                "<strong>%(user)s</strong>.",
                agent=self.name, user=user.name,
            ),
        )
        return {
            "ok": True,
            "model_name": model_name,
            "record_id": record_id,
            "user_id": user.id,
            "user_name": user.name,
            "previous_user_id": previous_user.id or None,
        }

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
            ``{'ok': True, 'activity_id': <id>, ...}`` on success;
            ``{'ok': True, 'skipped': True, 'reason': 'duplicate',
            'existing_activity_id': <id>, ...}`` when an equivalent
            open activity already exists;
            ``{'error': '<reason>'}`` on validation failure.
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
            return {"error": (
                "Model '%s' does not support activities (it does not "
                "inherit mail.activity.mixin)." % model_name
            )}
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
        existing_domain = [
            ("res_model", "=", model_name),
            ("res_id", "=", record.id),
            ("activity_type_id", "=", act_type.id),
            ("user_id", "=", assignee.id),
        ]
        if target_summary:
            existing_domain.append(("summary", "=", target_summary))
        existing = self.env["mail.activity"].search(existing_domain, limit=1)
        if existing:
            return {
                "ok": True,
                "skipped": True,
                "reason": "duplicate",
                "model_name": model_name,
                "record_id": record.id,
                "activity_id": existing.id,
                "existing_activity_id": existing.id,
                "activity_type_id": act_type.id,
                "user_id": assignee.id,
            }

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
            _(
                "🤖 <strong>%(agent)s</strong> scheduled activity "
                "<strong>%(summary)s</strong> for <strong>%(user)s</strong> "
                "on <strong>%(deadline)s</strong>.",
                agent=self.name,
                summary=target_summary or act_type.name or _("(no summary)"),
                user=assignee.name,
                deadline=deadline.strftime("%Y-%m-%d"),
            ),
        )
        return {
            "ok": True,
            "model_name": model_name,
            "record_id": record.id,
            "activity_id": activity.id,
            "activity_type_id": act_type.id,
            "summary": target_summary or act_type.name or "",
            "date_deadline": deadline.strftime("%Y-%m-%d"),
            "user_id": assignee.id,
            "user_name": assignee.name,
        }

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
