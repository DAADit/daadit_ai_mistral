# Security & Multi-Tenant Considerations — `daadit_ai_mistral`

A frank assessment of how this module handles credentials, user data, and
multi-tenant isolation. Read before deploying to a database that holds
data from more than one customer.

## Trust boundaries at a glance

```
  ┌───────────────────────────────────────────┐
  │              Odoo database                │
  │                                           │
  │   ai.agent  ──────────► daadit_ai_mistral │
  │       │                  ──┬──            │
  │       │                    │              │
  │       │ tool_calls         │ HTTPS        │
  │       ▼                    ▼              │
  │   _ai_tool_*          api.mistral.ai      │
  │   (stock methods)         (external)      │
  │                                           │
  │   ir.config_parameter                     │
  │     daadit_ai_mistral.mistral_key         │
  │       (plaintext, group_system)           │
  │                                           │
  └───────────────────────────────────────────┘
```

Two things leave the database boundary:
1. The text content of every chat message (prompt + response) goes to
   Mistral AI's servers in France/EU.
2. The text content of every embedded source document (PDF, knowledge
   base article) goes to Mistral's `/embeddings` endpoint when a
   Mistral-embedding agent processes it.

Customers who handle EU-personal data should confirm Mistral's DPA
covers their use case. The module does not anonymize, redact, or
otherwise transform content before sending.

## Multi-tenant posture (database = tenant)

This is the **safe** model: each customer has their own Odoo database.
- Every database has its own `ir.config_parameter` row for the Mistral
  key, so cross-customer key reuse is impossible.
- Every database has its own `ai.agent` records, `ai.embedding`
  vectors, and `daadit_ai_mistral.usage` history.
- No code in this module reads from "the other database" — there's no
  cross-tenant query path at all.

If your hosting setup is one Odoo instance per customer (Odoo.sh,
self-hosted-per-tenant, etc.) you are isolated by Odoo itself. This
module adds nothing to that isolation but doesn't subvert it either.

## Multi-company posture (one database, multiple companies)

This is the **caveat-laden** model: several customers / brands share
one Odoo database, separated by `res.company` records.

### What stock Odoo does

Stock `ai.agent` has **no** `company_id` field, no `company_ids`. So
agents are visible to every user on every company, and an agent's
configured topics + tools are not company-scoped either. That is a
stock-Odoo limitation, not something this module introduces.

### What this module does

- **Tool dispatch runs as the calling user.** As of v3.8.0,
  `_resolve_agent` re-browses the agent on the env's regular user
  (drops sudo) before dispatching `_ai_tool_*` methods. Each `search`
  / `read_group` query from the model therefore respects:
  - Odoo record rules,
  - the user's group memberships,
  - the user's `allowed_company_ids`.
  An agent on company A cannot be tricked into reading company B's
  records *via this module's dispatch path*, because the underlying
  ORM call has the user's access rights.
- **`daadit_ai_mistral.usage` rows are company-scoped.** The model
  carries `company_id` and a record rule restricts non-admin users to
  their own rows; admins see only their company's rows.

### What this module does **NOT** fix

- **Stock's lack of agent-level company scoping.** A user from
  company A can still chat with the company-default agent and ask
  it to search records — those searches are filtered by their access
  rights, but the agent's configuration (topic_ids, system prompt)
  is global. Agents can't be hidden from specific companies.
- **The Mistral API key is shared per database.** If two companies
  share an Odoo database, they also share the Mistral bill. There is
  one key in `ir.config_parameter`. To bill per company, deploy
  separate databases.

### Per-agent model access control (v3.9.0)

A stricter scope than what stock RBAC alone provides. Two M2M fields
on `ai.agent`:

- **Allowed models** (`daadit_allowed_model_ids`) — if populated, the
  agent can ONLY query models in this list. Empty = unrestricted.
- **Blocked models** (`daadit_blocked_model_ids`) — models the agent
  must never query, regardless of the allowed list. Blocked always
  wins over allowed.

The check fires in `tool_dispatch.run_tool_call` BEFORE the stock
`_ai_tool_*` method is invoked, so the model name never reaches Odoo's
ORM if the agent is denied. Mistral receives a structured error with
the list of permitted models, so it can re-strategize ("only these are
allowed, try one of those") rather than giving up.

Recommended baseline blocklist for any agent:

```
res.users, ir.config_parameter, ir.attachment, mail.message,
ir.logging, ir.cron, ir.model, ir.model.access, ir.actions.server,
res.users.log, auth.session.expired
```

Recommended allowlist patterns by role:

| Agent role | Allowed models |
|---|---|
| Sales | `sale.order`, `sale.order.line`, `crm.lead`, `res.partner`, `product.template`, `product.product` |
| Operations | `stock.picking`, `stock.move`, `stock.quant`, `product.template`, `mrp.production` |
| Finance read-only | `account.move`, `account.move.line`, `account.account`, `res.partner` |
| HR | `hr.employee`, `hr.department`, `hr.leave`, `hr.attendance` (skip `res.users`!) |
| General Q&A | (empty) — fall back to RBAC alone |

Reasonable practice: configure both lists. The allowlist defines the
agent's positive scope; the blocklist is a defensive layer for
sensitive models that should never appear via AI under any
circumstance.

### Recommendation

If you have customers in the same database who *cannot* see each
other's record metadata, run `_ai_tool_*` searches with `read_group`
+ a tight per-company domain at the agent's system prompt. Better:
deploy each customer in their own database.

## Credential handling

| What | Where | Who can read |
|---|---|---|
| Mistral API key | `ir.config_parameter` row `daadit_ai_mistral.mistral_key` | Users in `base.group_system` (Settings) |
| Storage format | Plain text | — |
| In transit | TLS (HTTPS) | — |
| Settings UI display | Masked (`password="True"` on the field) | — |

The key is **not encrypted at rest**. Anyone with database-superuser
SQL access (or `base.group_system` and the Technical menu) can read
it. This matches how stock Odoo stores OpenAI / Google keys and is the
common practice for `ir.config_parameter`-based secrets in the
ecosystem; treat it as a known limitation.

If you need stronger protection:
1. Restrict `base.group_system` membership tightly.
2. Audit access to `ir.config_parameter` via Odoo's audit trail
   (`mail.tracking` is not on it by default — wire it up if needed).
3. Rotate the Mistral key on offboarding events (export from
   `console.mistral.ai` → revoke old key).

## RBAC matrix for `daadit_ai_mistral.usage`

| Group | Read | Write | Create | Unlink | Visibility |
|---|---|---|---|---|---|
| `base.group_user` (internal user) | ✓ | ✗ | ✗ | ✗ | Only their own rows |
| `base.group_system` (Settings) | ✓ | ✓ | ✓ | ✓ | Their company's rows + global rows |

`record_usage` always uses `sudo()` to write the row (the chat user
might not have create rights). The row's `user_id` and `company_id`
default to the current env, so the row reports who actually ran the
call, not "Administrator".

## Logging & PII

- **Tool call args + results** are NOT logged to `ir.logging` by
  default. They may contain partner names, amounts, or free-text from
  the user's question. To opt in for audit purposes:
  ```
  ir.config_parameter:
    daadit_ai_mistral.log_tool_results = True
  ```
- **Tool errors** (`PARSE_ERROR`, `TYPEERROR`, `UNKNOWN_TOOL`,
  `RAISED`) are always logged at WARNING / ERROR — they're rare and
  necessary for diagnosis, but they include the call args and the
  Python exception text. Don't enable this in a database that holds
  highly regulated data without legal review.
- **`UserError` stack-trace tap** is OFF by default (since v3.7.1).
  Enable per-database via `daadit_ai_mistral.diag_trace_user_errors`.
- **Standard Python `_logger`** lines (`_logger.info`, `_logger.warning`)
  go to Odoo's stdout, where Odoo.sh / your deployment's log aggregator
  decides retention. We log model id and token counts at INFO; we do
  *not* log message content.

## Data sent to Mistral

Each chat call sends:
- The agent's system prompt (`ai.agent.system_prompt`).
- The current chat history (the messages list stock builds — this can
  include attachments, summaries, prior assistant replies).
- The list of tool definitions (function names + JSON schemas + the
  tool descriptions).
- The user's current question.

Each embedding call sends:
- The text content of the source chunk (stored in `ai.embedding.content`).

Mistral's terms of service govern retention of that data. As of writing
(May 2026), Mistral states API content is not used for model training
unless the customer opts in via the Console. Verify on
<https://console.mistral.ai/> before relying on this for compliance.

## Revoking access

`uninstall_hook` resets every Mistral `llm_model` value back to
`gpt-4o` and every `mistral-embed` back to `text-embedding-3-small`.
Embedding vectors generated under Mistral remain in
`ai.embedding.embedding_vector` after uninstall — they're 1024-dim
floats and unusable by stock OpenAI's 1536-dim or Google's 768-dim.
Run a cleanup: `UPDATE ai_embedding SET embedding_vector = NULL,
has_embedding_generation_failed = TRUE` to force regeneration on the
new provider.

The Mistral API key in `ir.config_parameter` is **not** removed on
uninstall (uninstalling a module doesn't remove its config params by
default in Odoo). Delete it manually:

```sql
DELETE FROM ir_config_parameter
 WHERE key LIKE 'daadit_ai_mistral.%';
```

Or in the Odoo UI: Settings → Technical → Parameters → System →
filter on `daadit_ai_mistral.*`.

## Reporting a security issue

Email **security@daadit.group** with reproduction steps. We aim to
acknowledge within two business days.
