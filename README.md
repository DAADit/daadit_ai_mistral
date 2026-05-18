# DAADit AI — Mistral Provider

Adds **Mistral AI** as a third LLM provider for Odoo 19 Enterprise's built-in
`ai` module, with feature parity to OpenAI (ChatGPT) and Google (Gemini).

## What it does

### Chat completions (`ai.agent`)

- Adds Mistral models to the `ai.agent` LLM Model dropdown:
  - `mistral-large-latest`, `mistral-medium-latest`, `mistral-small-latest`
  - `codestral-latest` (code-specialised)
  - `pixtral-large-latest` (vision)
  - `ministral-8b-latest`, `ministral-3b-latest` (edge / cheap)
- Routes any agent whose `llm_model` is a Mistral option to
  `https://api.mistral.ai/v1/chat/completions` instead of Odoo IAP.
- Maps the agent's **Response Style** (analytical / balanced / creative) to a
  Mistral `temperature` (`0.2 / 0.6 / 0.9`) unless the caller overrides it.

### Tool calling / Topics (`ai.topic` + server actions)

- Passes `tools` and `tool_choice` through to Mistral when an agent has
  `topic_ids` configured.
- Auto-builds OpenAI-compatible tool definitions from each topic's
  `ir.actions.server` records (`_daadit_build_tool_definitions`) so existing
  topics like "Natural Language Search", "Information retrieval" and
  "Create Leads" work out of the box.
- Returns `tool_calls` in the unified response shape so downstream code that
  dispatches actions sees the same envelope it would from OpenAI.

### Embeddings / Sources (`ai.embedding`)

- Adds `mistral-embed` to the `ai.embedding` Embedding Model dropdown
  alongside `text-embedding-3-small` (OpenAI) and `gemini-embedding-001`
  (Google).
- Routes embedding generation for any record using `mistral-embed` to
  `https://api.mistral.ai/v1/embeddings`.
- Batches in groups of 96 inputs per call.
- Writes vectors back into `embedding_vector` and clears
  `has_embedding_generation_failed` on success; sets it on failure.

### Settings UI

- Adds a **Mistral AI** provider block under **Settings → General Settings → AI**,
  matching the visual style of the existing ChatGPT and Gemini provider blocks
  (toggle → key field that appears when enabled, plus a link to
  `console.mistral.ai`).
- Extra advanced fields: base URL (for proxies / regional endpoints) and
  request timeout.
- All values stored in `ir.config_parameter` under the
  `daadit_ai_mistral.*` namespace; only visible to users in the
  `base.group_system` group.

## Install

This is a custom Odoo module — install via Odoo.sh or a local Odoo build, not
through XML-RPC.

1. Push the `daadit_ai_mistral` folder into your Odoo.sh repo, e.g. under
   `enterprise_custom_addons/` or wherever your custom addons live.
2. Commit and push to your dev / staging branch first.
3. On Odoo.sh, watch the build. Once green, open the database and update the
   apps list, then install **DAADit AI — Mistral Provider**. The `ai_app`
   module is pulled in automatically as a dependency, which itself pulls
   `ai`, `ai_app`, `ai_crm`, `ai_documents`, etc.
4. Open **Settings → General Settings → AI**, toggle **Use your own Mistral
   AI account**, paste the key from <https://console.mistral.ai/>, save.
5. Edit (or create) an `ai.agent`, set **LLM Model** to one of the Mistral
   options, save, and try a prompt.

## ⚠️ Recovery: "No provider found for the selected model" at registry load

If a build ever fails with this in the log:

```
File "/home/odoo/src/enterprise/ai/utils/llm_providers.py", line 72, in get_provider
    raise UserError(env._("No provider found for the selected model"))
```

…during loading of `enterprise/ai/data/ai_agent_data.xml`, the database has
a Mistral value in `ai_agent.llm_model` that stock can't process. Stock's
data load runs *before* our `_inherit` extensions are merged into
`ai.agent`, so our `_get_provider` override isn't yet in effect.

**Unblock**: connect to the DB and run

```sql
UPDATE ai_agent
   SET llm_model = 'gpt-4o'
 WHERE llm_model LIKE 'mistral%'
    OR llm_model LIKE 'codestral%'
    OR llm_model LIKE 'pixtral%'
    OR llm_model LIKE 'ministral%';

UPDATE ai_embedding
   SET embedding_model = 'text-embedding-3-small'
 WHERE embedding_model = 'mistral-embed';
```

…then trigger a rebuild. After this, the module's `uninstall_hook` and
`pre_init_hook` (added in v3.6.3) will keep the DB clean across future
install/uninstall cycles, so this only ever needs to be run by hand once.

## How the provider→embedding lookup is handled

Odoo Enterprise's `ai` module is closed-source. We don't know the exact
method that raises `"No embedding model found for the selected provider"`
when an `ai.agent` is saved with a non-stock provider, so v3.4.0 takes a
defensive three-layer approach:

1. **Static candidate overrides** for likely method names
   (`_get_embedding_model_for_provider`, `_get_default_embedding_model`,
   etc.). Whichever matches stock takes effect; the rest are dead methods.
2. **`_register_hook` bytecode scan** — at registry build time we walk
   every method on the merged `ai.agent` and `ai.embedding` classes, find
   any whose code constants contain the error string, and shadow it with
   a wrapper that returns `"mistral-embed"` for Mistral agents and
   delegates to the original for everything else.
3. **`_register_hook` class-dict patch** — we also look for class-level
   dicts that map provider names (`openai`, `google`) to embedding model
   ids and add `"mistral": "mistral-embed"` in place. If stock uses a
   simple dict lookup (the most common pattern), this alone fixes the
   error without needing to wrap any method.

Each layer logs what it found at INFO level under the `daadit_ai_mistral`
logger, so a single deploy is enough to confirm which mechanism stock
actually uses.

### Verifying the dispatch hookpoints

This module overrides several plausible method names on `ai.agent` and
`ai.embedding` and lets Odoo's MRO route the right one. Whichever exists
in the Enterprise version takes effect; the others are dead methods.

**On `ai.agent`** (chat completions), in priority order:

```
_get_llm_response  →  _call_llm  →  _make_llm_request
```

**On `ai.embedding`** (embedding generation):

```
_generate_embeddings  →  _compute_embeddings  →  _create_embeddings  →  _call_embedding_api
```

After the first successful end-to-end run, identify the real method name and
simplify by removing the unused candidates. To find the real one on your
Odoo.sh dev branch:

```bash
# Chat completions
grep -nE "(api\\.openai\\.com|generativelanguage|chat/completions)" \
    odoo/addons/ai/models/*.py

# Embeddings
grep -nE "(text-embedding-3-small|/embeddings|embedding-001)" \
    odoo/addons/ai/models/*.py
```

If none of the candidates match the real method, the install still succeeds
and the API key UI works — only the actual call will fall through to Odoo
IAP and error. Add the real method name as an additional override and push
again.

## Test checklist

Recommended order after deploying to **BroStar-Staging**:

### Chat completions
- [ ] Module installs without errors.
- [ ] **Settings → AI** shows the Mistral provider block matching the
      ChatGPT/Gemini style.
- [ ] Toggling the boolean and saving persists `daadit_ai_mistral.*` in
      `ir.config_parameter` (verify via `Technical → Parameters → System`).
- [ ] An `ai.agent` set to `mistral-small-latest` returns a reply when
      invoked through chatter "Ask AI" / AI composer / AI fields / systray.
- [ ] Logs show `Mistral call ok: model=… tokens=…/… tool_calls=…` lines.
- [ ] Switching that agent back to `gpt-4o` still works (OpenAI path
      untouched).

### Tool calling / Topics
- [ ] An agent with `topic_ids = [Natural Language Search]` set to a Mistral
      model still answers natural-language record queries.
- [ ] Logs show `tool_calls=N` with N>0 when the model decides to invoke a
      server action.
- [ ] The "Ask AI" agent (`is_ask_ai_agent=True`) on Mistral can still open
      list/kanban/pivot views via `AI: Open Menu *` server actions.

### Embeddings / Sources
- [ ] Add a Source (PDF / knowledge article) to a Mistral agent.
- [ ] The "AI Embedding: Generate Embeddings" server action runs without
      `has_embedding_generation_failed` being set.
- [ ] Asking the agent a question whose answer is in the source returns a
      relevant reply citing the chunk.

## Known limitations

- **Streaming** is implemented in the Mistral client (`stream=True`) but the
  Odoo `ai` module almost certainly consumes responses in one shot, so the
  current dispatch wrapper does not pass streaming through. Add when there's
  a real use case.
- **Vision (`pixtral-large-latest`)** requires `messages` with a list-of-parts
  `content`. `MistralClient.extract_text` handles that response shape, but
  the *inbound* message envelope from Odoo's `ai` module is unknown until
  the dispatch method's signature is confirmed.
- **Tool argument schemas** are exposed as a generic `{input: object}` to the
  model. Odoo's stock `ai` module probably builds richer per-action schemas;
  once the real builder method is identified, prefer overriding *that* over
  the fallback in `_daadit_build_tool_definitions`.

## Files

```
daadit_ai_mistral/
├── __manifest__.py
├── __init__.py
├── README.md
├── .gitignore
├── models/
│   ├── __init__.py
│   ├── res_config_settings.py    ← key + base_url + timeout settings
│   ├── ai_agent.py                ← LLM selection + chat dispatch + tool builder
│   └── ai_embedding.py            ← embedding selection + embedding dispatch
├── services/
│   ├── __init__.py
│   └── mistral_client.py          ← HTTP client (chat + embeddings)
└── views/
    └── res_config_settings_views.xml   ← Provider UI block
```

## License

LGPL-3 — same convention as the other DAADit / BroStar custom modules.
