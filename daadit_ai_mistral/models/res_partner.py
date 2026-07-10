# -*- coding: utf-8 -*-
"""GDPR Art. 17 (right to erasure) helper for Mistral usage history.

When a partner exercises the right to erasure, we must remove or
anonymise the personal data we hold about them. ``mistral_usage`` rows
hold ``user_id`` (which links back to a partner). This action wipes
those links and adds a pseudonym so the cost-aggregation queries that
group by user keep working without identifying anyone.

Sister to ``daadit_mcp_multi_tenant.res.partner.action_mcp_gdpr_erase``
— the two modules each provide their own button for clarity. If both
modules are installed the partner form has two buttons; running both
covers the full DAADit footprint.
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = "res.partner"

    def action_daadit_ai_gdpr_erase(self):
        """Anonymise ``mistral_usage`` rows tied to this partner's
        users. Keeps the cost figures intact (token counts and USD
        estimates) but removes the user identifier and stamps the row
        with the erasure date for audit purposes.
        """
        self.ensure_one()
        # Permission: gated by the view's `groups` attribute.
        # mistral_usage rows live on `daadit_ai_mistral.usage`.
        if "daadit_ai_mistral.usage" not in self.env:
            raise UserError(_(
                "The `daadit_ai_mistral.usage` model is not registered "
                "— is the module properly installed?"
            ))
        Usage = self.env["daadit_ai_mistral.usage"].sudo()

        users = self.env["res.users"].sudo().search(
            [("partner_id", "=", self.id)]
        )
        if not users:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Mistral usage — GDPR erasure"),
                    "message": _(
                        "No internal user is linked to %s — nothing to "
                        "anonymise on the usage history."
                    ) % self.display_name,
                    "type": "warning",
                },
            }
        rows = Usage.search([("user_id", "in", users.ids)])
        if not rows:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Mistral usage — GDPR erasure"),
                    "message": _(
                        "No usage rows reference %(name)s "
                        "(%(users)s linked user(s)) — nothing to do."
                    ) % {"name": self.display_name, "users": len(users)},
                    "type": "warning",
                },
            }
        # Anonymise: drop user_id, leave token counts + cost intact for
        # aggregate cost reporting.
        rows.write({"user_id": False})
        when = fields.Datetime.now().strftime("%Y-%m-%d")
        _logger.info(
            "daadit_ai_mistral.gdpr_erase: partner=%s users=%s "
            "usage_rows=%s wiped on %s",
            self.id, len(users), len(rows), when,
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Mistral usage — GDPR erasure"),
                "message": _(
                    "Anonymised %(rows)s Mistral usage row(s) for "
                    "%(name)s. Token counts and cost figures kept "
                    "(no PII), user link removed."
                ) % {"rows": len(rows), "name": self.display_name},
                "type": "success",
                "sticky": True,
            },
        }
