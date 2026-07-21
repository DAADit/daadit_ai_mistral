# -*- coding: utf-8 -*-
"""Token-usage tracking for the Mistral provider.

Each chat or embedding call records one row in ``daadit_ai_mistral.usage``
with the agent / channel / user / model / token counts and an estimated
cost. The model is intentionally append-only from the user's perspective
(``ondelete='cascade'`` from ai.agent so cleanup follows agent removal,
no manual deletion via the UI).

Pricing
-------
Per-million-token prices for input/output are stored in a module-level
constant ``_PRICING_USD_PER_1M``. They're a snapshot of Mistral's
public list price at the time of writing — keep them roughly accurate
but don't rely on this module for invoicing. Override via
``ir.config_parameter`` if needed:

    daadit_ai_mistral.price.<model>.input    -> USD per 1M input tokens
    daadit_ai_mistral.price.<model>.output   -> USD per 1M output tokens

Currency is USD by default; the ``estimated_cost`` field stores the raw
USD value and the company's main currency is used for display.
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


# Pricing snapshot — USD per 1,000,000 tokens (input, output).
# Source: mistral.ai pricing page. May lag — override via ICP.
_PRICING_USD_PER_1M = {
    "mistral-large-latest":   (2.0, 6.0),
    "mistral-medium-latest":  (0.4, 2.0),
    "mistral-small-latest":   (0.2, 0.6),
    "codestral-latest":       (0.2, 0.6),
    "pixtral-large-latest":   (2.0, 6.0),
    "ministral-8b-latest":    (0.1, 0.1),
    "ministral-3b-latest":    (0.04, 0.04),
    # Embeddings have a single price (input only) per Mistral pricing.
    "mistral-embed":          (0.1, 0.0),
}


def _get_unit_price(env, model_id, kind):
    """Return USD-per-1M-tokens for ``kind`` ('input' or 'output').

    Lookup order: ICP override → static dict. Returns 0.0 when
    unknown so we never error out on a billing-only field.
    """
    icp = env["ir.config_parameter"].sudo()
    key = f"daadit_ai_mistral.price.{model_id}.{kind}"
    raw = icp.get_param(key)
    if raw:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    pair = _PRICING_USD_PER_1M.get(model_id)
    if not pair:
        return 0.0
    return pair[0] if kind == "input" else pair[1]


class MistralUsage(models.Model):
    _name = "daadit_ai_mistral.usage"
    _description = "Mistral API Usage Record"
    _order = "create_date desc, id desc"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=False)

    # --- Provenance ----------------------------------------------------
    agent_id = fields.Many2one(
        "ai.agent",
        string="AI Agent",
        ondelete="cascade",
        index=True,
    )
    channel_id = fields.Many2one(
        "discuss.channel",
        string="Channel",
        ondelete="set null",
        index=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="User",
        default=lambda self: self.env.user,
        ondelete="set null",
        index=True,
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        default=lambda self: self.env.company,
        index=True,
    )

    # --- Call metadata -------------------------------------------------
    kind = fields.Selection(
        [("chat", "Chat completion"),
         ("embedding", "Embedding")],
        string="Kind",
        required=True,
        default="chat",
    )
    model = fields.Char(string="Model", required=True, index=True)
    iterations = fields.Integer(
        string="Iterations",
        help="Number of round-trips to Mistral (≥2 means tool calls).",
        default=1,
    )
    has_tools = fields.Boolean(string="Used tools")
    error = fields.Text(string="Error", help="Empty when call succeeded.")
    # v19.0.4.8.0: per-conversation accounting. One user question (or
    # scheduled run) = exactly one depth-0 row; router sub-calls
    # (depth > 0) share the parent's turn_uuid. Count questions as
    # depth-0 rows; group on turn_uuid for exact per-question cost.
    depth = fields.Integer(
        string="Router depth", default=0, readonly=True,
        help="0 = a direct (top-level) request — exactly one per user "
             "question or scheduled run. >0 = a router sub-call "
             "executed inside another request.",
    )
    turn_uuid = fields.Char(
        string="Turn", index=True, readonly=True,
        help="Shared by every API call made while answering one "
             "question (top-level call + router sub-calls).",
    )

    # --- Tokens --------------------------------------------------------
    prompt_tokens = fields.Integer(string="Prompt tokens")
    completion_tokens = fields.Integer(string="Completion tokens")
    total_tokens = fields.Integer(string="Total tokens", compute="_compute_total_tokens", store=True)

    # --- Cost ----------------------------------------------------------
    unit_input_usd = fields.Float(
        string="Input price (USD/1M)",
        digits=(12, 6),
    )
    unit_output_usd = fields.Float(
        string="Output price (USD/1M)",
        digits=(12, 6),
    )
    estimated_cost_usd = fields.Float(
        string="Estimated cost (USD)",
        digits=(12, 6),
        compute="_compute_estimated_cost",
        store=True,
    )

    # ------------------------------------------------------------------
    @api.depends("prompt_tokens", "completion_tokens")
    def _compute_total_tokens(self):
        for r in self:
            r.total_tokens = (r.prompt_tokens or 0) + (r.completion_tokens or 0)

    @api.depends("prompt_tokens", "completion_tokens",
                 "unit_input_usd", "unit_output_usd")
    def _compute_estimated_cost(self):
        for r in self:
            r.estimated_cost_usd = (
                (r.prompt_tokens or 0) * (r.unit_input_usd or 0) / 1_000_000
                + (r.completion_tokens or 0) * (r.unit_output_usd or 0) / 1_000_000
            )

    @api.depends("model", "create_date", "kind")
    def _compute_display_name(self):
        for r in self:
            r.display_name = f"[{r.kind}] {r.model or '?'} @ {r.create_date or '?'}"

    # ------------------------------------------------------------------
    @api.model
    def record_usage(self, *, kind="chat", model=None, agent_id=None,
                     channel_id=None, prompt_tokens=0, completion_tokens=0,
                     iterations=1, has_tools=False, error=None,
                     depth=0, turn_uuid=None):
        """Convenience constructor used by the Mistral dispatch helpers.

        Best-effort: a logging hiccup must never break the chat flow,
        so we wrap creation in a savepoint and swallow exceptions.
        """
        try:
            unit_in = _get_unit_price(self.env, model or "", "input")
            unit_out = _get_unit_price(self.env, model or "", "output")
            vals = {
                "kind": kind,
                "model": model or "?",
                "agent_id": agent_id,
                "channel_id": channel_id,
                "prompt_tokens": prompt_tokens or 0,
                "completion_tokens": completion_tokens or 0,
                "iterations": iterations or 1,
                "has_tools": bool(has_tools),
                "error": (error or "")[:8000] if error else False,
                "unit_input_usd": unit_in,
                "unit_output_usd": unit_out,
                "depth": depth or 0,
                "turn_uuid": turn_uuid or False,
            }
            with self.env.cr.savepoint():
                self.sudo().create(vals)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral.usage: record_usage failed for "
                "model=%s agent=%s — swallowed", model, agent_id,
            )
