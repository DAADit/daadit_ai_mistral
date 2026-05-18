# Changelog

All notable changes to `daadit_ai_mistral`. Versions follow Odoo's
`<odoo_major>.0.<feature>.<minor>.<patch>` scheme:

- **feature** bumps for new tools, models or controllers,
- **minor** for new fields, views or non-breaking schema changes,
- **patch** for bugfixes and v-specific compatibility tweaks.

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
