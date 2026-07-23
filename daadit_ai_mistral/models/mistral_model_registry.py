# -*- coding: utf-8 -*-
"""Live registry of Mistral models available via the Mistral API.

The ``ai.agent.llm_model`` selection is fed from this table (see
``ai_agent.py``) so newly-released Mistral models appear automatically
once the daily sync — or the manual "Refresh models" button — has run.
No code change or redeploy is needed when Mistral ships a new model.

A small hardcoded seed (``data/mistral_models_seed.xml``) keeps the
dropdown usable before the first successful sync (fresh install, no key
yet, or the API is unreachable).
"""
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class DaaditAiMistralModel(models.Model):
    _name = "daadit.ai.mistral.model"
    _description = "Mistral model (synced from Mistral API)"
    _order = "sequence, technical_name"
    # Without _rec_name Odoo's computed display_name falls back to
    # "model,id" — which is exactly what the agent dropdown then shows.
    _rec_name = "technical_name"

    technical_name = fields.Char(
        required=True,
        index=True,
        help="Model id sent to the Mistral API, e.g. 'mistral-large-latest'.",
    )
    label = fields.Char(
        help="Human-readable label shown in the agent model dropdown. "
             "(Named 'label' because 'display_name' is reserved by base "
             "and silently breaks the selection labels.)",
    )
    active = fields.Boolean(
        default=True,
        help="Untick to hide this model from the agent dropdown without "
             "deleting it (keeps existing agents that use it valid).",
    )
    sequence = fields.Integer(default=10)
    synced_from_api = fields.Boolean(
        string="From API",
        readonly=True,
        help="True when this row was created/updated by a sync with the "
             "Mistral API (as opposed to the built-in seed).",
    )
    last_synced = fields.Datetime(readonly=True)

    _sql_constraints = [
        ("technical_name_uniq", "unique(technical_name)",
         "This Mistral model id already exists in the registry."),
    ]

    @api.model
    def _selection_entries(self):
        """Return ``(value, label)`` tuples for the ``llm_model`` field.

        Active rows only, ordered by sequence. Empty list if the table
        has no active rows — the caller then falls back to the seed
        constant so the dropdown is never empty.
        """
        recs = self.sudo().search(
            [("active", "=", True)], order="sequence, technical_name",
        )
        return [(r.technical_name, r.label or r.technical_name)
                for r in recs]

    @api.model
    def _sync_from_api(self):
        """Fetch models from Mistral and upsert rows. Returns the count.

        Raises (via ``MistralClient.from_env``) if the Mistral key is not
        enabled — callers that must not crash (the cron) guard for that
        first.
        """
        from ..services.mistral_client import MistralClient

        client = MistralClient.from_env(self.env)
        models_data = client.list_models()
        now = fields.Datetime.now()
        touched = 0
        for seq, item in enumerate(models_data, start=1):
            mid = item["id"]
            vals = {
                "label": item.get("display_name") or mid,
                "synced_from_api": True,
                "last_synced": now,
                "active": True,
                "sequence": seq,
            }
            rec = self.sudo().search(
                [("technical_name", "=", mid)], limit=1,
            )
            if rec:
                rec.write(vals)
            else:
                self.sudo().create(dict(vals, technical_name=mid))
            touched += 1
        _logger.info(
            "daadit_ai_mistral: synced %d Mistral models from the API",
            touched,
        )
        return touched

    @api.model
    def _cron_sync_models(self):
        """Daily cron entry point. Never raises — a transient API error
        must not leave the scheduled action in a failed state."""
        ICP = self.env["ir.config_parameter"].sudo()
        if ICP.get_param("daadit_ai_mistral.mistral_key_enabled") not in (
            "True", "1", True,
        ):
            _logger.info(
                "daadit_ai_mistral: model sync skipped (Mistral key disabled)"
            )
            return False
        try:
            self._sync_from_api()
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_mistral: scheduled Mistral model sync failed"
            )
            return False
        return True

    def action_sync_now(self):
        """Manual refresh from the list view / settings button."""
        count = self._sync_from_api()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Mistral models refreshed"),
                "message": _(
                    "%s model(s) synced from the Mistral API.", count,
                ),
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
