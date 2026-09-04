# -*- coding: utf-8 -*-
from odoo import fields, models


class DaaditAiAgentSkill(models.Model):
    """Business-facing capability an agent can be sold/provisioned with."""

    _name = "daadit.ai.agent.skill"
    _description = "DAADit AI Agent Skill"
    _order = "sequence, category, name"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    category = fields.Selection(
        [
            ("finance", "Finance"),
            ("sales", "Sales"),
            ("service", "Service"),
            ("marketing", "Marketing"),
            ("operations", "Operations"),
            ("technical", "Technical"),
            ("governance", "Governance"),
            ("general", "General"),
        ],
        required=True,
        default="general",
    )
    risk_level = fields.Selection(
        [
            ("read_only", "Read-only"),
            ("concept_only", "Concept-only"),
            ("mandated_write", "Mandated write"),
            ("internal", "Internal"),
        ],
        required=True,
        default="read_only",
        help=(
            "Product-level guardrail. Mandated write skills may only be "
            "enabled for a customer after an explicit tenant mandate."
        ),
    )
    description = fields.Text(translate=True)
    required_apps = fields.Char(
        help="Comma-separated Odoo apps/modules the customer environment needs."
    )
    required_models = fields.Char(
        help="Comma-separated Odoo models this skill expects to use."
    )
    capability_codes = fields.Char(
        help=(
            "Comma-separated MCP capability codes needed for provisioning "
            "(for example search_read, create, write)."
        ),
    )
    # Taak 844 — wat provisioning uit één skill afleidt, naast de
    # required_* / capability_codes hierboven.
    blocked_models = fields.Char(
        help=(
            "Comma-separated Odoo models this skill must never touch on "
            "the customer tenant (merged into the provisioned agent's "
            "block list)."
        ),
    )
    field_blocklist = fields.Char(
        help=(
            "Comma-separated model.field entries this skill forbids "
            "(merged with the tenant field blocklist at provision time)."
        ),
    )
    activity_scope_models = fields.Char(
        help=(
            "Comma-separated models on which this skill may schedule "
            "mail.activity rows. Empty = no activity scope from this skill."
        ),
    )
    schedule_active_default = fields.Boolean(
        string="Schedule active after provision",
        default=False,
        help=(
            "If set, hire provisioning may activate the schedule "
            "immediately. Default off: schedules stay inactive / dry-run "
            "until connection and customer ack are complete (844/837)."
        ),
    )
    schedule_prompt = fields.Text(
        translate=True,
        help="Optional standing prompt fragment for this skill's schedule.",
    )


class AIAgent(models.Model):
    _inherit = "ai.agent"

    daadit_skill_ids = fields.Many2many(
        "daadit.ai.agent.skill",
        relation="daadit_ai_agent_skill_rel",
        column1="agent_id",
        column2="skill_id",
        string="Skills",
        help=(
            "Business-facing skills this agent can perform. These are the "
            "skills customers will eventually choose on uitzendkracht.ai; "
            "technical capabilities, model scopes and schedules are derived "
            "from this catalog in the provisioning phase."
        ),
    )

    def _daadit_seed_skills(self):
        """Attach first catalog skills to known DAADit agents.

        Idempotent and additive: existing manually selected skills stay in
        place. Databases without these named agents simply skip them.
        """
        mapping = {
            "Argus": ["skill_governance_agent_assurance"],
            # Bo is de boekhouder, geen grootboekcontroleur: hij hoort
            # de hele boekhoudset te kennen, anders kan hij bij een klant
            # alleen naar het grootboek kijken en niets van de rest.
            "Bo": [
                "skill_finance_ledger_check",
                "skill_finance_overdue_receivables",
                "skill_finance_invoice_candidates",
                "skill_finance_margin_report",
                "skill_finance_month_report",
                "skill_finance_vat_prep",
            ],
            # Orderverwerking hoort bij sales: Sanne's werk loopt van
            # lead tot levering. Factureren blijft bij Marit, daarom
            # geeft ze een order klaar-om-te-factureren alleen door.
            "Sanne": [
                "skill_sales_order_intake_check",
                "skill_sales_order_price_variance",
                "skill_sales_order_stock_check",
                "skill_sales_order_delivery_watch",
                "skill_sales_order_invoice_handover",
            ],
            "Dirk": ["skill_finance_overdue_receivables"],
            "Marit": ["skill_finance_invoice_candidates"],
            "Coen": ["skill_finance_margin_report"],
            "Floris": [
                "skill_finance_margin_report",
                "skill_finance_month_report",
            ],
            "Eva": [
                "skill_finance_month_report",
                "skill_finance_vat_prep",
            ],
            "Hilda": ["skill_service_ticket_triage"],
            "Nora": ["skill_service_ticket_triage"],
            "Pim": ["skill_project_backlog"],
            "Lux": ["skill_website_visibility"],
        }
        for agent_name, xmlids in mapping.items():
            agent = self.search([("name", "=", agent_name)], limit=1)
            if not agent:
                continue
            skill_ids = []
            for xmlid in xmlids:
                skill = self.env.ref(
                    "daadit_ai_mistral.%s" % xmlid,
                    raise_if_not_found=False,
                )
                if skill:
                    skill_ids.append(skill.id)
            missing = [
                skill_id for skill_id in skill_ids
                if skill_id not in agent.daadit_skill_ids.ids
            ]
            if missing:
                agent.write({
                    "daadit_skill_ids": [(4, skill_id) for skill_id in missing],
                })
