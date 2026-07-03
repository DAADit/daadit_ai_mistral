# Changelog

All notable changes to `daadit_ai_mistral`. Versions follow Odoo's
`<odoo_major>.0.<feature>.<minor>.<patch>` scheme:

- **feature** bumps for new tools, models or controllers,
- **minor** for new fields, views or non-breaking schema changes,
- **patch** for bugfixes and v-specific compatibility tweaks.

## 19.0.4.1.0 — 2026-05-25

Chatter attribution for AI write-tools. Every autonomous change now
posts an internal note on the target record's chatter, attributed to
the agent's own partner — exactly like OdooBot's own messages appear
under "OdooBot". Without this, an operator who saw "Assigned to
Wytse" in a ticket's tracking had no way to tell whether Nick did
that manually or a scheduled agent did it (the write-tools' create_uid
is always the schedule's "Run as" user).

### Minor

* **`ai.agent._daadit_post_agent_message(record, body)`** — new helper
  that calls `record.message_post(author_id=self.partner_id.id,
  subtype_xmlid='mail.mt_note')`. No-op when the target doesn't
  inherit `mail.thread`. Swallows messaging errors — a chatter hiccup
  must not break the tool result.
* **`_ai_tool_assign_user`** posts on success: *"🤖 &lt;Agent&gt;
  assigned this record to &lt;User&gt;."*
* **`_ai_tool_schedule_activity`** posts on success: *"🤖 &lt;Agent&gt;
  scheduled activity &lt;Summary&gt; for &lt;User&gt; on &lt;Date&gt;."*

Skipped/duplicate cases (already_assigned, duplicate activity) do
NOT post — nothing happened, no note needed.

### Why the note and not just tracking?

Odoo's automatic tracking on `user_id` shows "Nick assigned this to
Wytse" — because `create_uid` on the write is Nick (the run-as user).
That's technically true but functionally misleading: the *decision*
came from an agent, not Nick. The chatter note surfaces the actual
actor while the audit metadata still records the underlying user.

## 19.0.4.0.0 — 2026-05-21

Adds two write-side AI tools so autonomous agents (e.g. the
helpdesk SLA-agent driven by `daadit_ai_agent_schedule`) can
actually ACT, not just report. Stock Odoo Enterprise's `ai` module
ships only read-only AI tools (Search, Read group, Get Fields,
Open Menu *) — without a write tool an autonomous agent is gated
at the dispatch layer regardless of how the prompt is written.

### New AI tools

* **`AI: Assign User`** — sets `user_id` on a record. Validates the
  target model has a `user_id` many2one to `res.users`, that the
  assignee is an active internal user, and short-circuits with
  `{'skipped': True, 'reason': 'already_assigned'}` when the user
  was already set (idempotent on retries).
* **`AI: Schedule Activity`** — creates a `mail.activity` on a
  record via `mail.activity.mixin`. Idempotent: if an open
  activity with the same activity_type + summary + assignee
  already exists on the record, returns
  `{'skipped': True, 'reason': 'duplicate', ...}` instead of
  duplicating. Defaults `activity_type` to
  `mail.mail_activity_data_todo`, deadline to today, assignee to
  the record's current `user_id` (or the calling user).

Both tools take `model_name`, so the agent's
`daadit_allowed_model_ids` / `daadit_blocked_model_ids` /
`daadit_field_blocklist` gates apply unchanged — the same access
controls that protect read tools now cover writes.

### Files changed

* `__manifest__.py` — version bump 19.0.3.14.0 → 19.0.4.0.0; new
  `data/ai_tools.xml` added to the data list.
* `data/ai_tools.xml` (new) — `ir.actions.server` records for both
  tools on `ai.model_ai_agent` with `noupdate="1"` so admins can
  attach them to custom `ai.topic`s without upgrade reverts.
* `models/ai_agent.py` — adds `_ai_tool_assign_user` and
  `_ai_tool_schedule_activity` on `ai.agent`. Returns
  `{'error': '...'}` JSON on validation failure so the LLM can
  recover; never raises into the chat loop.
* `services/tool_dispatch.py` — adds `TOOL_SCHEMAS` entries for
  both tools (Mistral needs proper parameter docs to call them
  correctly) and extends `_MODEL_REQUIRED_TOOLS` so missing
  `model_name` returns a crisp instruction instead of a TypeError.

### Why this is in `daadit_ai_mistral` (and not in a Helpdesk-specific module)

The two tools are intentionally generic: they take a `model_name`,
not a hardcoded model. Helpdesk is the immediate driver but any
agent over any model that has a `user_id` field / supports
activities can use them (CRM leads, sales orders, project tasks,
field-service tasks, …). Putting them next to the existing read
tools — backed by the same dispatch / allow-list / scrub gates
— keeps the surface area in one module.

## 19.0.3.14.0 — 2026-05-23

AI Fields & non-agent provider routing. Triggered by a real-world bug:
Studio AI Fields and "Update with AI" automation rules ignored the
selected Mistral agent entirely and unconditionally hit
`api.openai.com`, surfacing as either:

* `NotImplementedError` at
  `enterprise/ai/utils/llm_api_service.py:550` (the internal
  `_request_llm` dispatcher has no Mistral branch), or
* `Oeps, het antwoord kon niet worden verwerkt` (AI Fields parses the
  response as JSON; Mistral returned prose because we never asked for
  JSON-mode output).

Five hookpoints in `enterprise/ai/`'s production code call
`LLMApiService(...)` directly. Only one — `ai.agent._get_provider()` —
was covered by the previous patch. The other four (the bare
`LLMApiService(request.env)` in `controllers/agent.py`, the
`AI_PROVIDER = "openai"` constant in `ir_actions_server.py`, and two
embedding-call sites) all bypassed Mistral entirely. Plus the AI
Fields path lives in a separate `enterprise/ai_fields/` module that
hardcodes `llm_model="gpt-4o"`.

### Patch

* **`LLMApiService.__init__` wrapper now supports force-mode.** New
  system parameter `daadit_ai_mistral.force_provider` with values
  `off` / `auto` / `always`. In `auto` (recommended) any caller that
  passes no provider or `"openai"` is redirected to Mistral; explicit
  `"google"` keeps working. This routes AI Fields, automation
  actions, embedding lookups and any future direct caller through
  Mistral with one hook instead of patching each call site.
* **`LLMApiService._request_llm` (internal dispatcher) is now also
  patched.** The previous version only patched the public
  `request_llm`. AI Fields and any direct caller skip that and use
  the internal one — which on stock falls into
  `raise NotImplementedError()` for non-OpenAI/non-Google providers.
  The patched internal dispatcher routes to `_request_llm_mistral`
  when `self.provider == "mistral"` and returns a 4-tuple so callers
  unpacking `response, *__ = llm_api._request_llm(...)` (AI Fields)
  and 4-tuple-style callers both work.
* **AI Fields' `llm_model="gpt-4o"` is auto-substituted.** When the
  caller passes a non-Mistral model name and we are on the Mistral
  route, fall back to `daadit_ai_mistral.default_chat_model` (system
  parameter) or `mistral-medium-latest`.
* **Plural prompt kwargs supported.** AI Fields passes
  `system_prompts=[...]` and `user_prompts=[...]` as lists. The
  extractor now reads plural and singular forms, joining list items
  with double newlines before sending to Mistral.
* **JSON schema enforcement.** When the caller passes `schema=`
  (or `json_schema=`, `response_schema=`, `output_schema=`) we build
  Mistral's `response_format` payload. For `mistral-large-*`,
  `mistral-medium-*` and `ministral-*` we use strict
  `json_schema` mode with the caller's schema. For other models we
  degrade to `json_object` (valid JSON, no schema enforcement) and
  inject the schema into the system prompt as a hint. Tools are
  dropped when JSON mode is active because Mistral 400s when both
  are set simultaneously.

### Configuration

After upgrade, set the following system parameter to enable
non-agent routing:

* `daadit_ai_mistral.force_provider` = `auto` (recommended)

Optional:

* `daadit_ai_mistral.default_chat_model` = `mistral-medium-latest`
  (the Mistral model substituted when callers hardcode `gpt-4o`)
* `daadit_ai_mistral.force_provider_name` = `mistral` (only needed
  if a future addon installs another provider you want to use)

### Known limitations

* `web_grounding=True` (AI Fields) is silently dropped. Mistral has
  no built-in web search.
* Embeddings still go to OpenAI under `auto` mode (the embedding
  callers pass `provider="openai"` explicitly). Set
  `force_provider=always` to route embeddings through Mistral too,
  but verify your Mistral embedding pipeline works first — it is
  thinner-tested than the chat path.

## 19.0.3.13.2 — 2026-05-13

Tool-call context-explosion fix. Triggered by an end-user prompt
"geef me een overzicht van alle producten" against the "Ask AI" agent:
Mistral medium called `ir_actions_server_search` on `product.product`
without specifying `fields`, Odoo returned every column (including
`image_1920` as base64), `tool_dispatch.run_tool_call` forwarded the
unbounded result back into the conversation, and the next request to
`POST /v1/chat/completions` landed at 746,511 tokens — well over
mistral-medium's 131,072-token context — and was rejected with
`HTTP 400 too large for model with 131072 maximum context length`.

Three defence layers, in order of activation, all in
`services/tool_dispatch.py`:

### Patch

* **Default `fields=['id', 'display_name']` for search calls.** When
  the LLM omits `fields` on `ir_actions_server_search`, we inject the
  minimal pair instead of letting it default to "every column". Logged
  at INFO so we can see in `ir.logging` how often the LLM forgets to
  scope. The tool-schema description already nudges toward 3-6 fields;
  this is the enforcement.
* **Binary-field strip on every tool result.** New
  `_strip_binary_fields` walks the JSON-safe result and drops keys
  matching the Odoo binary-field conventions: `image` / `avatar`
  exact, `image_<N>` / `avatar_<N>` sized variants (128/256/512/1024/
  1920), `datas`, `raw`, `db_datas`, and anything ending in
  `_binary`. Base64 binary has no use in chat context and is the
  single biggest source of token bloat in tool results.
* **Hard result-size cap (`_MAX_TOOL_RESULT_CHARS = 50_000`).** After
  the binary strip, if the JSON-serialised result still exceeds 50k
  characters (~12-15k tokens), `_enforce_result_size_cap` returns a
  structured error dict to the LLM with the cap, the actual size,
  and explicit recovery instructions (narrower `fields`, tighter
  `domain`, smaller `limit`). The model gets to re-call rather than
  the whole turn dying with a Mistral 400. Logged at WARNING with
  the tool name + size so over-cap cases are visible in operational
  logs.

The three layers stack: layer 1 prevents the common case, layer 2
catches binary-field bloat regardless of `fields` argument (covers
`read_group` results that aggregate by an image-keyed groupby, or
custom topic actions that don't take `fields`), and layer 3 is the
final hard ceiling on anything that slips through.

## 19.0.3.13.1 — 2026-05-13

Module icon swap. Replaces the previous DAADit-branded gradient icon
with the Mistral mark so the module is instantly recognisable in the
Apps list next to the sibling DAADit AI provider modules
(`daadit_ai_copilot`, `daadit_ai_claude`). Also syncs the
`__init__.py` startup log version string, which was still echoing
`v19.0.3.12.0` after the `.13.0` bump.

### Patch

* **New `static/description/icon.png` (512×512) and
  `icon@256.png` (256×256).** Asset-only change; install and upgrade
  are no-ops apart from the file replacement. Sourced as PNG at the
  same canvas proportions as the prior icon so it slots cleanly into
  Odoo's Apps grid and the Settings → AI provider list.
* **`__init__.py` startup log version string.** Bumped from
  `v19.0.3.12.0` to `v19.0.3.13.1` so the operational log line matches
  the actual installed version after upgrade.

## 19.0.3.12.0 — 2026-05-10

Onboarding-UX release. No code/schema changes; the General Settings →
AI page now teaches admins where each provider's API key comes from,
so first-time setup doesn't bounce through Google for "what's a
Mistral console".

### Minor

* **Provider key help-card.** Added a collapsible "Where do I get an
  API key for these providers?" info card at the top of the AI
  providers block in General Settings. Each of ChatGPT, Gemini,
  Mistral and Microsoft 365 Copilot (Azure OpenAI) gets a 3–5 step
  walkthrough with direct console links. The card is admin-only
  (`groups="base.group_system"`) and uses native `<details>`
  collapse so it stays out of the way once the admin is familiar.
  Lives in an `xpath position="before"` on the `ai_providers` block
  so it doesn't touch the OpenAI/Gemini/Azure `<setting>` blocks we
  don't own.
* **Inline Mistral key hint.** Below the Mistral key field, a small
  muted hint links straight to `console.mistral.ai/api-keys` with
  the shortest possible recipe (sign in → billing → Create new key).
  Duplicates info from the top card on purpose: admins who collapse
  the card still see the key step right where they paste.

## 19.0.3.11.0 — 2026-05-10

Security-hardening release. Implements every High and Medium finding
from the v19.0.3.10.0 audit, plus the relevant Low items. No breaking
schema changes, but two new fields on `ai.agent` and a new
`@api.constrains` on `res.config.settings` — both safe to upgrade in
place.

### High

* **H1 — Allowlist for the Mistral base URL.** `mistral_base_url`
  now passes through `ResConfigSettings._validate_base_url` on every
  save AND on every `MistralClient.from_env(...)` call. Rules:
  scheme must be `https://`, host must be on a small allowlist
  (`api.mistral.ai`, `codestral.mistral.ai` by default), no userinfo,
  no IP literals. Admins who need a regional endpoint or a corporate
  proxy extend the allowlist via ICP
  `daadit_ai_mistral.allowed_base_url_hosts` (comma-separated). This
  blocks the SSRF + key-exfiltration vector where a tampered base
  URL would have caused the Bearer header to ship to an arbitrary
  host. Defense-in-depth: both the settings UI and the runtime
  factory validate, so an `ir.config_parameter` poke that bypasses
  the UI is also caught.
* **H2 — Redacted first-call diagnostic log.**
  `_log_first_call_args` previously dumped the first chat's full
  args + kwargs (incl. user message text capped at 600 chars) at
  WARNING level once per worker. Operational logs at Odoo.sh have
  long retention and aren't under typical GDPR scope, so PII landing
  there was a real leak. The function now logs only structural
  info — types, lengths, dict keys — never values. The historical
  forensic purpose (finding the real stock kwarg names) has been
  served since v3.6.x.
* **H3 — Threadlocal cleared between requests.**
  `tool_dispatch.current_agent` was set in `ai.agent._get_provider`
  but never cleared. In a gevent / async worker that interleaves
  requests on one thread, a previous Mistral chat's agent record
  could leak into a subsequent request. The patched `request_llm`
  now wraps the Mistral call in `try / finally` and clears
  `current_agent.record = None` after every dispatch, regardless
  of outcome.
* **H4 — Removed legacy `_daadit_call_mistral` chat path.** The
  pre-LLMApiService dispatch path (`_get_llm_response` /
  `_call_llm` / `_make_llm_request` candidate overrides feeding
  into `_daadit_call_mistral`) reached the Mistral API without
  going through the per-agent allow/block-list, the field-level
  blocklist, the domain-validation gate, or the threadlocal
  cleanup. On Odoo 19 stock chat goes through `LLMApiService` so
  the path was unreachable in normal use, but a third-party module
  calling one of those candidate names directly would have
  silently bypassed every security gate. Deleted; all Mistral
  traffic now flows through the audited
  `LLMApiService.request_llm` ⇒ `_request_llm_mistral` ⇒
  `tool_dispatch.run_tool_call` chain. ~150 lines removed from
  `models/ai_agent.py`.

### Medium

* **M1 — Field blocklist now covers `groupby` / `aggregates` /
  `having`.** Previously a blocked field (e.g. `res.partner.vat`)
  was rejected in `domain` filters but not in `read_group` calls
  that grouped by the field — the result rows were keyed by the
  field's actual values, letting the LLM recover them by inspecting
  the response. The check in `tool_dispatch.run_tool_call` now
  rejects any `groupby`, `aggregates`, or `having` clause that
  references a blocked field, with or without a `:operator` /
  `:interval` suffix.
* **M2 — Loud warning when PII-logging flag is on.** When
  `daadit_ai_mistral.log_tool_results = True`, every Mistral tool
  call's args + result get persisted to `ir.logging` (capped at
  1500 chars but unredacted). Admins who flipped the flag for a
  debug session sometimes forgot to flip it back. Each Python
  worker now logs a one-shot WARNING the first time it observes
  the flag is on, so the live setting is visible in operational
  logs.
* **M3 — Trimmed 4xx response logging.** The HTTP-level WARNING
  on a Mistral 4xx no longer echoes `resp.text[:1500]` or the
  redacted payload. We log only status, path, model, message
  count, tool count, and the parsed `detail` string (capped at
  300 chars). Mistral occasionally echoes request fragments back
  in its `detail`; the cap is short enough that a stray PII
  fragment can't sit in operational logs for long.

### Low

* **L2 — Mistral request timeout is now constrained to
  1 ≤ t ≤ 600 seconds.** A 0 timeout (silently treated as "no
  timeout" by the `requests` library) would let a stalled
  connection wedge an Odoo worker forever. A timeout > 600 is
  almost certainly a mistake. Both checks live in
  `ResConfigSettings._check_mistral_timeout` and as a runtime
  clamp inside `MistralClient.__init__`.
* **L3 — Field-blocklist syntax validation.** A new
  `@api.constrains` on `daadit_field_blocklist` rejects entries
  that aren't shaped `model.field` (lowercase, dot-separated). It
  also issues a WARNING (no error) when the model exists but the
  field doesn't, since multi-DB deployments may have different
  apps installed.
* **L4 — Explicit boolean for the temperature override.** Added
  `daadit_mistral_temperature_active` (default False). The float
  field now applies only when the boolean is ticked, so a
  legitimate `0.0` (deterministic mode) is honoured instead of
  being treated as "unset" and silently overridden by the
  response_style mapping.
* **L5 — Allowlist for `extra` in chat completions.** The
  convenience `extra` kwarg now only forwards a known set of
  Mistral-API-documented fields (`top_p`, `random_seed`,
  `safe_prompt`, `response_format`, …). Any other key is dropped
  with a debug log, preventing a misbehaving caller from
  overriding `messages` / `tools` / `model`.

### Migration notes

* The new `daadit_mistral_temperature_active` boolean defaults
  to `False`, so existing agents continue to behave exactly as
  before (the temperature is set by `response_style`). Tick the
  new checkbox to activate the override.
* `mistral_base_url` values currently in your database that are
  off-allowlist will be rejected on the next save. If you legitimately
  need a custom endpoint, set
  `ir.config_parameter`
  `daadit_ai_mistral.allowed_base_url_hosts` BEFORE saving the
  settings page.
