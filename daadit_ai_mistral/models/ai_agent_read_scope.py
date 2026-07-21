# -*- coding: utf-8 -*-
"""Hard per-agent read scope (Fase 0-gate, hard lees-domein).

The WRITE side of agent governance is already hard: the editable tool
wrappers guard every mutation server-side. The READ side only had the
model allow/block lists — an agent allowed on ``knowledge.article``
could read *every* article. A ``daadit.ai.agent.read.scope`` line pins
the records an agent may see on one model: its domain is AND-ed into
every ``search`` / ``read_group`` the dispatcher executes for that
agent (``tool_dispatch.run_tool_call``), below the prompt layer, so no
prompt drift or clever domain can widen the read window.

Example — Vault Agent restricted to the knowledge tree under article
191::

    model:  knowledge.article
    domain: [["root_article_id", "=", 191]]

Models WITHOUT a scope line stay governed by the allow/block lists
alone; the scope narrows records *within* a model the agent may query.
"""
import json
import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class AiAgentReadScope(models.Model):
    _name = "daadit.ai.agent.read.scope"
    _description = "AI Agent — Hard Read Scope"
    _order = "agent_id, model_name"

    agent_id = fields.Many2one(
        "ai.agent", string="AI Agent", required=True,
        ondelete="cascade", index=True,
    )
    model_id = fields.Many2one(
        "ir.model", string="Model", required=True, ondelete="cascade",
        help="The Odoo model this scope line restricts.",
    )
    model_name = fields.Char(
        related="model_id.model", string="Model name",
        store=True, index=True,
    )
    domain = fields.Char(
        string="Record domain", required=True, default="[]",
        help="Odoo domain (JSON) AND-ed into every search/read_group "
             "this agent runs on the model — enforced in the tool "
             "layer, not the prompt. Example: "
             '[["root_article_id", "=", 191]]',
    )
    active = fields.Boolean(string="Active", default=True)

    # Odoo 19 constraint style — ``_sql_constraints`` is no longer
    # supported (the registry warns and silently skips it).
    _agent_model_uniq = models.Constraint(
        "UNIQUE(agent_id, model_id)",
        "There is already a read-scope line for this model on this "
        "agent — edit that line instead of adding a second one.",
    )

    @api.constrains("domain", "model_id")
    def _check_domain(self):
        """Reject scope domains that would fail at dispatch time.

        The dispatcher fails CLOSED on an unusable scope domain (a
        hard gate must never silently widen), so a broken domain would
        block every read on the model. Catch it at save time instead:
        the domain must parse as a JSON list and pass a real
        ``search_count`` against the model.
        """
        for rec in self:
            try:
                parsed = json.loads(rec.domain or "[]")
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    "Read-scope domain is not valid JSON: %s" % exc
                )
            if not isinstance(parsed, list):
                raise ValidationError(
                    "Read-scope domain must be a JSON array of Odoo "
                    "domain conditions, e.g. "
                    '[["root_article_id", "=", 191]].'
                )
            model_name = rec.model_id.model
            if model_name not in self.env:
                raise ValidationError(
                    "Model '%s' is not available in this database."
                    % model_name
                )
            try:
                self.env[model_name].sudo().search_count(parsed, limit=1)
            except Exception as exc:  # noqa: BLE001
                raise ValidationError(
                    "Read-scope domain is not a valid domain for "
                    "'%s': %s" % (model_name, exc)
                )


class AiAgent(models.Model):
    _inherit = "ai.agent"

    daadit_read_scope_ids = fields.One2many(
        "daadit.ai.agent.read.scope", "agent_id",
        string="Hard read scope",
        help="Per-model record domains enforced on every search this "
             "agent runs. Models without a line here stay governed by "
             "the allowed/blocked model lists alone.",
    )

    def _daadit_read_scope_domain(self, model_name):
        """Return the parsed scope domain for ``model_name``, or None.

        None = no active scope line for the model (unrestricted within
        the model allow/block lists). Raises ValueError when a scope
        line exists but its stored domain cannot be parsed — the
        dispatcher treats that as fail-closed.
        """
        self.ensure_one()
        if not model_name:
            return None
        line = self.daadit_read_scope_ids.filtered(
            lambda s: s.active and s.model_name == model_name
        )[:1]
        if not line:
            return None
        parsed = json.loads(line.domain or "[]")
        if not isinstance(parsed, list):
            raise ValueError(
                "read-scope domain for %s is not a list" % model_name
            )
        return parsed
