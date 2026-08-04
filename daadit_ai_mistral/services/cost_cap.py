# -*- coding: utf-8 -*-
"""Daily spend circuit-breaker for the Mistral provider.

Every Mistral chat call is priced into ``daadit_ai_mistral.usage``
(stored ``estimated_cost_usd`` per call, per agent). This module reads
that table to enforce a HARD daily budget: once today's spend reaches
the cap, further Mistral calls are refused with a clear, translated
message and an admin is notified once per day.

Config (all ``ir.config_parameter``, tunable without a code deploy):

    daadit_ai_mistral.daily_cost_cap_usd      float; 0 = disabled.
                                              The cap, in the usage
                                              table's currency (USD —
                                              Mistral bills in USD).
    daadit_ai_mistral.cost_cap_notify_email   recipient for the "budget
                                              reached" mail. Empty →
                                              falls back to the admin
                                              user / company email.

Design notes
------------
* **Soft ceiling, by design.** The check sums ALREADY-RECORDED usage
  before a call runs; the call that crosses the line still completes
  (its cost is booked afterwards), and every call after it is blocked.
  That's exactly what you want for runaway protection — it halts the
  2nd/3rd/... expensive call, not just the last one.
* **Single choke point.** The check runs at the top of
  ``_request_llm_mistral``, which every Mistral chat call (concierge,
  specialist, routed sub-run, scheduled agent) funnels through — so one
  gate covers them all.
* **Never breaks the chat flow.** Any error in the cap logic (DB read,
  mail send) is swallowed; a broken breaker must not take down the AI.
"""
import logging
from datetime import datetime, time

_logger = logging.getLogger(__name__)

_CAP_ICP = "daadit_ai_mistral.daily_cost_cap_usd"
_NOTIFY_EMAIL_ICP = "daadit_ai_mistral.cost_cap_notify_email"
_NOTIFIED_ON_ICP = "daadit_ai_mistral.cost_cap_notified_on"


def _today_start_utc(env):
    """Naive-UTC datetime for local midnight (start of 'today').

    Odoo stores ``create_date`` as naive UTC. We anchor 'today' to the
    user/company timezone so the budget resets at local midnight, then
    convert back to naive UTC for the domain comparison.
    """
    tzname = env.context.get("tz") or (
        env.user.tz if env.user else None
    ) or "Europe/Amsterdam"
    try:
        import pytz
        tz = pytz.timezone(tzname)
        now_local = datetime.now(tz)
        start_local = tz.localize(
            datetime.combine(now_local.date(), time.min)
        )
        return start_local.astimezone(pytz.utc).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        # Fallback: naive UTC midnight. Good enough if pytz/tz misbehave.
        now = datetime.utcnow()
        return datetime.combine(now.date(), time.min)


def get_cap(env):
    """Return the configured daily cap as a float, or 0.0 when unset /
    disabled / unparseable."""
    try:
        raw = env["ir.config_parameter"].sudo().get_param(_CAP_ICP, "0")
        cap = float(str(raw).strip().replace(",", "."))
        return cap if cap > 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def daily_spend(env):
    """Sum of ``estimated_cost_usd`` for usage booked since local
    midnight. Cheap: aggregated read on an indexed ``create_date``."""
    start = _today_start_utc(env)
    try:
        groups = env["daadit_ai_mistral.usage"].sudo().read_group(
            [("create_date", ">=", start.strftime("%Y-%m-%d %H:%M:%S"))],
            ["estimated_cost_usd:sum"],
            [],
        )
        if groups:
            return float(groups[0].get("estimated_cost_usd") or 0.0)
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.cost_cap: failed to sum daily spend; "
            "treating as 0 (fail-open so chat is never blocked by a "
            "broken breaker)"
        )
    return 0.0


def check(env):
    """Return ``(blocked, spent, cap)``.

    ``blocked`` is True when a cap is configured and today's spend has
    reached it. When blocked, an admin notification is sent at most once
    per day. Fail-open: any internal error yields ``blocked=False``.
    """
    try:
        cap = get_cap(env)
        if not cap:
            return (False, 0.0, 0.0)
        spent = daily_spend(env)
        if spent >= cap:
            _notify_once(env, spent, cap)
            return (True, spent, cap)
        return (False, spent, cap)
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.cost_cap: check() raised; failing open"
        )
        return (False, 0.0, 0.0)


def _notify_once(env, spent, cap):
    """Email the configured admin that the daily budget is reached —
    once per day, guarded by a date marker in ir.config_parameter."""
    try:
        icp = env["ir.config_parameter"].sudo()
        today = _today_start_utc(env).strftime("%Y-%m-%d")
        if icp.get_param(_NOTIFIED_ON_ICP) == today:
            return  # already told them today

        recipient = (icp.get_param(_NOTIFY_EMAIL_ICP) or "").strip()
        if not recipient:
            admin = env.ref("base.user_admin", raise_if_not_found=False)
            recipient = (
                (admin and admin.partner_id.email)
                or (env.company.email or "")
            ).strip()
        if not recipient:
            _logger.warning(
                "daadit_ai_mistral.cost_cap: daily budget reached "
                "(%.2f/%.2f) but no notify recipient configured "
                "(set %s).", spent, cap, _NOTIFY_EMAIL_ICP,
            )
            icp.set_param(_NOTIFIED_ON_ICP, today)
            return

        body = (
            "<p>Het AI-dagbudget is bereikt.</p>"
            "<ul>"
            "<li><strong>Besteed vandaag:</strong> $%.2f</li>"
            "<li><strong>Dagbudget:</strong> $%.2f</li>"
            "</ul>"
            "<p>De AI-assistent (Ask AI, de specialist-agents en geplande "
            "agents) is voor de rest van vandaag automatisch gepauzeerd. "
            "Het budget reset vannacht om middernacht.</p>"
            "<p>Wil je het budget verhogen? Pas de systeemparameter "
            "<code>%s</code> aan (Instellingen &rarr; Technisch &rarr; "
            "Systeemparameters).</p>"
        ) % (spent, cap, _CAP_ICP)

        mail = env["mail.mail"].sudo().create({
            "subject": "DAADit AI: dagbudget bereikt — assistent gepauzeerd",
            "body_html": body,
            "email_to": recipient,
            "auto_delete": True,
        })
        mail.send()
        icp.set_param(_NOTIFIED_ON_ICP, today)
        _logger.warning(
            "daadit_ai_mistral.cost_cap: daily budget reached "
            "(%.2f/%.2f) — notified %s and paused AI for today.",
            spent, cap, recipient,
        )
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.cost_cap: notification failed; the cap "
            "still blocks, only the e-mail didn't send"
        )


def agent_state(env, agent):
    """Budgetstand van één agent, of None als die niet te bepalen is.

    De teller zelf leeft in ``daadit_ai_agent_schedule`` omdat daar de
    grenzen per agent en per vestiging staan; deze module vraagt hem
    alleen op. Staat die module niet geïnstalleerd, dan blijft alleen
    de systeembrede dagcap hierboven over — dat is de oude situatie en
    dus veilig.

    Fail-open: chat mag nooit stukgaan op een kapotte meter.
    """
    if not agent:
        return None
    try:
        if "daadit.ai.agent.schedule" not in env:
            return None
        return env["daadit.ai.agent.schedule"].sudo()._agent_budget_state(
            agent.sudo()
        )
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_mistral.cost_cap: agent_state raised for agent "
            "%s; failing open", getattr(agent, "id", None),
        )
        return None


def fair_use_notice_en(state):
    """English source for the fair-use line shown above the answer.

    Returns an empty string when there is nothing to say — below the
    threshold the customer should not be bothered at all.
    """
    if not state or not state.get("message"):
        return ""
    return state["message"]


def should_drop_tools(state):
    """True when this agent may still talk but no longer act.

    A hard stop that also kills the conversation reads as an outage:
    the customer asks something, gets nothing, and calls support.
    Keeping the answer while dropping the tools costs one cheap
    completion instead of a runaway tool chain, and the customer hears
    *why* his colleague suddenly cannot do things.
    """
    return bool(state and state.get("blocked"))


def blocked_message_en(spent, cap):
    """English source for the user-facing 'budget reached' message.
    Translated into the chat language by the caller."""
    return (
        "_(The AI assistant has reached today's usage budget "
        "($%.2f of $%.2f) and is paused until tomorrow. Contact an "
        "administrator to raise the daily budget.)_" % (spent, cap)
    )
