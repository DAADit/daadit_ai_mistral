# -*- coding: utf-8 -*-
"""Per-agent / per-tenant AI budgets with fair-use warnings.

``services/cost_cap.py`` already had ONE global daily ceiling per
provider. That protects the database against a runaway loop, but it
says nothing about the economics of a subscription: an agent sold at a
fixed price per month has a cost budget of its own, and a customer
(company) has a budget over all their agents together.

A ``daadit.ai.budget`` line pins a daily and/or monthly USD ceiling on
one scope:

    scope=agent    one ``ai.agent`` — "this colleague costs at most X"
    scope=company  one ``res.company`` — the tenant ceiling over every
                   agent of that customer
    scope=global   the whole database (equivalent of the old ICP cap)

Spend is read from the provider usage tables (Mistral + Claude when
installed), so a budget covers BOTH providers — an agent that switches
model can't escape its ceiling.

Three levels, evaluated before every chat call:

    < warn_ratio            silent
    >= warn_ratio           fair-use notice appended to the answer
    >= 1.0                  hard stop; the call is refused

The notice is appended at most once per day per budget, so a user gets
warned rather than nagged, and the admin is mailed once per day per
budget.

Fail-open by design: any error while evaluating budgets leaves the
chat working. A broken meter must never take the AI down.
"""
import logging
from datetime import datetime, time

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

_USAGE_MODELS = (
    "daadit_ai_mistral.usage",
    "daadit_ai_claude.usage",
)


def _period_start_utc(env, monthly=False):
    """Naive-UTC datetime for the start of today / this month, local."""
    tzname = env.context.get("tz") or (
        env.user.tz if env.user else None
    ) or "Europe/Amsterdam"
    try:
        import pytz
        tz = pytz.timezone(tzname)
        now_local = datetime.now(tz)
        date_local = now_local.date()
        if monthly:
            date_local = date_local.replace(day=1)
        start_local = tz.localize(datetime.combine(date_local, time.min))
        return start_local.astimezone(pytz.utc).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        now = datetime.utcnow()
        date_utc = now.date().replace(day=1) if monthly else now.date()
        return datetime.combine(date_utc, time.min)


class DaaditAiBudget(models.Model):
    _name = "daadit.ai.budget"
    _description = "AI Budget & Fair Use"
    _order = "scope, id"

    name = fields.Char(required=True)
    scope = fields.Selection(
        [("agent", "Per agent"),
         ("company", "Per company (tenant)"),
         ("global", "Whole database")],
        required=True, default="agent",
    )
    agent_id = fields.Many2one(
        "ai.agent", string="AI Agent", ondelete="cascade", index=True,
    )
    company_id = fields.Many2one(
        "res.company", string="Company", ondelete="cascade", index=True,
    )
    daily_cap_usd = fields.Float(
        string="Daily cap (USD)", digits=(12, 2), default=0.0,
        help="0 = no daily ceiling on this scope.",
    )
    monthly_cap_usd = fields.Float(
        string="Monthly cap (USD)", digits=(12, 2), default=0.0,
        help="0 = no monthly ceiling on this scope. This is the one "
             "that protects the margin on a fixed monthly price.",
    )
    warn_ratio = fields.Float(
        string="Fair-use warning at", default=0.6,
        help="Fraction of the cap at which the user gets a fair-use "
             "notice in the chat (0.6 = at 60%).",
    )
    notify_email = fields.Char(
        string="Notify",
        help="Who to mail when this budget warns or blocks. Empty "
             "falls back to daadit_ai_mistral.cost_cap_notify_email.",
    )
    active = fields.Boolean(default=True)

    spend_today_usd = fields.Float(
        string="Spent today (USD)", digits=(12, 4),
        compute="_compute_spend", help="All providers combined.",
    )
    spend_month_usd = fields.Float(
        string="Spent this month (USD)", digits=(12, 4),
        compute="_compute_spend", help="All providers combined.",
    )
    usage_ratio = fields.Float(
        string="Budget used", compute="_compute_spend",
        help="Highest of the daily and monthly ratios; 1.0 = blocked.",
    )

    @api.constrains("scope", "agent_id", "company_id")
    def _check_scope_target(self):
        for rec in self:
            if rec.scope == "agent" and not rec.agent_id:
                raise ValidationError(
                    "A per-agent budget needs an AI Agent."
                )
            if rec.scope == "company" and not rec.company_id:
                raise ValidationError(
                    "A per-company budget needs a company."
                )

    @api.constrains("warn_ratio")
    def _check_warn_ratio(self):
        for rec in self:
            if not 0 < rec.warn_ratio <= 1:
                raise ValidationError(
                    "The fair-use warning threshold must be between 0 "
                    "and 1 (0.6 = warn at 60% of the budget)."
                )

    @api.depends("scope", "agent_id", "company_id",
                 "daily_cap_usd", "monthly_cap_usd")
    def _compute_spend(self):
        for rec in self:
            rec.spend_today_usd = rec._spend(monthly=False)
            rec.spend_month_usd = rec._spend(monthly=True)
            ratios = []
            if rec.daily_cap_usd:
                ratios.append(rec.spend_today_usd / rec.daily_cap_usd)
            if rec.monthly_cap_usd:
                ratios.append(rec.spend_month_usd / rec.monthly_cap_usd)
            rec.usage_ratio = max(ratios) if ratios else 0.0

    # ------------------------------------------------------------------
    # Spend
    # ------------------------------------------------------------------
    def _spend_domain(self, start):
        """Domain selecting the usage rows this budget is accountable
        for, since ``start``."""
        self.ensure_one()
        domain = [("create_date", ">=", start.strftime("%Y-%m-%d %H:%M:%S"))]
        if self.scope == "agent":
            domain.append(("agent_id", "=", self.agent_id.id))
        elif self.scope == "company":
            domain.append(("company_id", "=", self.company_id.id))
        return domain

    def _spend(self, monthly=False):
        """Summed ``estimated_cost_usd`` over every installed provider.

        Fail-open: a provider whose table can't be read counts as 0
        rather than blocking the chat.
        """
        self.ensure_one()
        start = _period_start_utc(self.env, monthly=monthly)
        domain = self._spend_domain(start)
        total = 0.0
        for model_name in _USAGE_MODELS:
            if model_name not in self.env:
                continue
            try:
                groups = self.env[model_name].sudo().read_group(
                    domain, ["estimated_cost_usd:sum"], [],
                )
                if groups:
                    total += float(
                        groups[0].get("estimated_cost_usd") or 0.0
                    )
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "daadit.ai.budget: could not read spend from %s for "
                    "budget %s — counting 0", model_name, self.id,
                )
        return total

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    @api.model
    def _budgets_for(self, agent=None):
        """Active budgets that apply to this call, most specific first."""
        domain = ["|", "|",
                  ("scope", "=", "global"),
                  ("scope", "=", "company"),
                  ("agent_id", "=", agent.id if agent else False)]
        budgets = self.sudo().search(domain)
        company = self.env.company
        return budgets.filtered(
            lambda b: b.scope != "company" or b.company_id == company
        )

    @api.model
    def evaluate(self, agent=None):
        """Return ``(blocked, warning, detail)`` for the next call.

        ``blocked``  — True when any applicable budget is exhausted; the
                       message is then the hard-stop text.
        ``message``  — English source of the user-facing notice (the
                       provider translates it into the chat language),
                       or "" when there is nothing to say.
        ``detail``   — dict for logging: budget, period, spent, cap.
        """
        try:
            for budget in self._budgets_for(agent):
                state = budget._state()
                if not state:
                    continue
                level, period, spent, cap = state
                if level == "block":
                    budget._notify(period, spent, cap, blocking=True)
                    return (True, budget.blocked_text_en(
                        period, spent, cap), {
                        "budget": budget.display_name, "period": period,
                        "spent": spent, "cap": cap,
                    })
                if level == "warn" and budget._may_warn_today():
                    budget._notify(period, spent, cap, blocking=False)
                    return (False, budget._fair_use_text_en(
                        period, spent, cap), {
                        "budget": budget.display_name, "period": period,
                        "spent": spent, "cap": cap,
                    })
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit.ai.budget: evaluate() raised; failing open"
            )
        return (False, "", {})

    def _state(self):
        """``("block"|"warn", period, spent, cap)`` or None when fine.

        The monthly ceiling is checked first: on a fixed monthly price
        that's the budget that decides the margin, and its message is
        the one the customer needs to see.
        """
        self.ensure_one()
        checks = [
            ("month", self.monthly_cap_usd, lambda: self._spend(True)),
            ("day", self.daily_cap_usd, lambda: self._spend(False)),
        ]
        best = None
        for period, cap, get_spend in checks:
            if not cap:
                continue
            spent = get_spend()
            if spent >= cap:
                return ("block", period, spent, cap)
            if spent >= cap * (self.warn_ratio or 0.6) and best is None:
                best = ("warn", period, spent, cap)
        return best

    # -- messaging ------------------------------------------------------
    def _fair_use_text_en(self, period, spent, cap):
        """English source for the in-chat fair-use notice."""
        self.ensure_one()
        window = "this month" if period == "month" else "today"
        return (
            "_(Fair use: this AI colleague has used $%.2f of its $%.2f "
            "budget for %s. It keeps working, but at 100%% it pauses "
            "until the budget resets. Ask your administrator if you "
            "need a larger budget.)_" % (spent, cap, window)
        )

    def blocked_text_en(self, period, spent, cap):
        """English source for the hard-stop message."""
        window = "this month" if period == "month" else "today"
        reset = (
            "on the 1st of next month" if period == "month" else "tomorrow"
        )
        return (
            "_(This AI colleague has reached its usage budget for %s "
            "($%.2f of $%.2f) and is paused until %s. Contact your "
            "administrator to raise the budget.)_"
            % (window, spent, cap, reset)
        )

    def _warn_marker_key(self):
        self.ensure_one()
        return "daadit.ai.budget.warned.%s" % self.id

    def _may_warn_today(self):
        """True when this budget hasn't warned yet today (one notice
        per day, so a user is warned rather than nagged)."""
        self.ensure_one()
        icp = self.env["ir.config_parameter"].sudo()
        today = _period_start_utc(self.env).strftime("%Y-%m-%d")
        if icp.get_param(self._warn_marker_key()) == today:
            return False
        icp.set_param(self._warn_marker_key(), today)
        return True

    def _notify(self, period, spent, cap, blocking=False):
        """Mail the operator once per day per budget."""
        self.ensure_one()
        try:
            icp = self.env["ir.config_parameter"].sudo()
            today = _period_start_utc(self.env).strftime("%Y-%m-%d")
            marker = "daadit.ai.budget.notified.%s.%s" % (
                self.id, "block" if blocking else "warn",
            )
            if icp.get_param(marker) == today:
                return
            recipient = (self.notify_email or icp.get_param(
                "daadit_ai_mistral.cost_cap_notify_email") or "").strip()
            if not recipient:
                recipient = (self.env.company.email or "").strip()
            if not recipient:
                icp.set_param(marker, today)
                return
            window = "deze maand" if period == "month" else "vandaag"
            state = "GEBLOKKEERD" if blocking else "waarschuwing"
            body = (
                "<p>AI-budget <strong>%s</strong> — %s.</p>"
                "<ul>"
                "<li><strong>Besteed %s:</strong> $%.2f</li>"
                "<li><strong>Budget:</strong> $%.2f</li>"
                "</ul>"
                "<p>%s</p>"
            ) % (
                self.display_name, state, window, spent, cap,
                ("De agent is gepauzeerd tot het budget reset."
                 if blocking else
                 "De gebruiker heeft een fair-use melding gekregen in "
                 "de chat. Bij 100% pauzeert de agent."),
            )
            mail = self.env["mail.mail"].sudo().create({
                "subject": "DAADit AI: budget %s — %s" % (
                    state.lower(), self.display_name),
                "body_html": body,
                "email_to": recipient,
                "auto_delete": True,
            })
            mail.send()
            icp.set_param(marker, today)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit.ai.budget: notification failed for budget %s; "
                "the budget itself still applies", self.id,
            )
